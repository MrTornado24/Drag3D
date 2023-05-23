import math
import torch
import torch.nn.functional as F
import numpy as np
import dearpygui.dearpygui as dpg
from scipy.spatial.transform import Rotation as R
import nvdiffrast.torch as dr
import time
import click

from mesh import Mesh

import dnnlib
from torch_utils.ops import upfirdn2d
from torch_utils.ops import bias_act
from torch_utils.ops import filtered_lrelu
from torch_utils.ops import conv2d_gradfix
from torch_utils.ops import grid_sample_gradfix

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True  # Improves training speed.
torch.backends.cuda.matmul.allow_tf32 = True  # Improves numerical accuracy.
torch.backends.cudnn.allow_tf32 = True  # Improves numerical accuracy.

upfirdn2d._init()
bias_act._init()
filtered_lrelu._init()
conv2d_gradfix.enabled = True  # Improves training speed.
grid_sample_gradfix.enabled = True  # Avoids errors with the augmentation pipe.

def safe_normalize(x, eps=1e-20):
    return x / torch.sqrt(torch.clamp(torch.sum(x * x, -1, keepdim=True), min=eps))

class OrbitCamera:
    def __init__(self, W, H, r=2, fovy=60, near=0.1, far=10):
        self.W = W
        self.H = H
        self.radius = r # camera distance from center
        self.fovy = fovy # in degree
        self.near = near
        self.far = far
        self.center = np.array([0, 0, 0], dtype=np.float32) # look at this point
        self.rot = R.from_matrix(np.eye(3))
        self.up = np.array([0, 1, 0], dtype=np.float32) # need to be normalized!

    # pose
    @property
    def pose(self):
        # first move camera to radius
        res = np.eye(4, dtype=np.float32)
        res[2, 3] = self.radius # opengl convention...
        # rotate
        rot = np.eye(4, dtype=np.float32)
        rot[:3, :3] = self.rot.as_matrix()
        res = rot @ res
        # translate
        res[:3, 3] -= self.center
        return res

    # view
    @property
    def view(self):
        return np.linalg.inv(self.pose)
    
    # intrinsics
    @property
    def intrinsics(self):
        focal = self.H / (2 * np.tan(np.radians(self.fovy) / 2))
        return np.array([focal, focal, self.W // 2, self.H // 2], dtype=np.float32)

    # projection (perspective)
    @property
    def perspective(self):
        y = np.tan(np.radians(self.fovy) / 2)
        aspect = self.W / self.H
        return np.array([[1/(y*aspect),    0,            0,              0], 
                         [           0,  -1/y,            0,              0],
                         [           0,    0, -(self.far+self.near)/(self.far-self.near), -(2*self.far*self.near)/(self.far-self.near)], 
                         [           0,    0,           -1,              0]], dtype=np.float32)

    
    def orbit(self, dx, dy):
        # rotate along camera up/side axis!
        side = self.rot.as_matrix()[:3, 0] # why this is side --> ? # already normalized.
        rotvec_x = self.up * np.radians(-0.05 * dx)
        rotvec_y = side * np.radians(-0.05 * dy)
        self.rot = R.from_rotvec(rotvec_x) * R.from_rotvec(rotvec_y) * self.rot

    def scale(self, delta):
        self.radius *= 1.1 ** (-delta)

    def pan(self, dx, dy, dz=0):
        # pan in camera coordinate system (careful on the sensitivity!)
        self.center += 0.0005 * self.rot.as_matrix()[:3, :3] @ np.array([dx, -dy, dz])

class GET3DWrapper:
    def __init__(self, device, G_kwargs, resume_pretrain):

        self.device = device

        common_kwargs = dict(c_dim=0, img_resolution=1024, img_channels=3)
        G_kwargs['device'] = device
        self.G = dnnlib.util.construct_class_by_name(**G_kwargs, **common_kwargs).train().requires_grad_(False).to(device)  # subclass of torch.nn.Module
        
        print('[INFO] resume GET3D from pretrained path %s' % (resume_pretrain))
        model_state_dict = torch.load(resume_pretrain, map_location=device)

        # we only need the ema model
        self.G.load_state_dict(model_state_dict['G_ema'], strict=True)

        # freeze it!
        self.G.eval()
        for p in self.G.parameters():
            p.requires_grad_(False)
        
    @torch.no_grad()
    def generate(self, geo_z=None, tex_z=None, use_style_mixing=False):

        if geo_z is None:
            geo_z = torch.randn([1, self.G.z_dim], device=self.device)
        
        if tex_z is None:
            tex_z = torch.randn([1, self.G.z_dim], device=self.device)
        
        
        # break down generation
        # generated_mesh = self.G.generate_3d_mesh(geo_z=geo_z, tex_z=tex_z, c=None, truncation_psi=0.7, use_style_mixing=use_style_mixing)
        ws = self.G.mapping(tex_z, None, truncation_psi=0.7, truncation_cutoff=None, update_emas=False) # [1, 9, 512]
        ws_geo = self.G.mapping_geo(geo_z, None, truncation_psi=0.7, truncation_cutoff=None, update_emas=False) # [1, 22, 512]
        v, f, vt, ft, tex = self.G.synthesis.extract_3d_shape(ws, ws_geo)

        # TODO: ws_geo is what we want to "drag"! [1, 22, 512]
        # should supervise from tri-plane space, and let it backprop to ws_geo...

        # process tex 
        albedo = (tex[0].permute(1,2,0).contiguous() + 1) / 2

        # build mesh object
        mesh = Mesh.load(v=v[0].float().contiguous(), f=f[0].int().contiguous(), vt=vt[0].float().contiguous(), ft=ft[0].int().contiguous(), albedo=albedo.float().contiguous(), device=self.device)
        # mesh.auto_normal()

        return mesh, geo_z, tex_z

    
class GUI:
    def __init__(self, opt, debug=True):
        self.opt = opt # shared with the trainer's opt to support in-place modification of rendering parameters.
        self.W = opt.W
        self.H = opt.H
        self.cam = OrbitCamera(opt.W, opt.H, r=opt.radius, fovy=opt.fovy)
        self.bg_color = torch.ones(3, dtype=torch.float32) # default white bg
        self.render_buffer = np.zeros((self.W, self.H, 3), dtype=np.float32)
        self.need_update = True # camera moved, should reset accumulation
        self.light_dir = np.array([0, 0])
        self.ambient_ratio = 0.5
        self.mode = 'lambertian'
        self.debug = debug

        self.glctx = dr.RasterizeCudaContext() # dr.RasterizeGLContext()

        # load model
        self.device = torch.device('cuda')
        self.model = GET3DWrapper(self.device, G_kwargs=opt.G_kwargs, resume_pretrain=opt.resume_pretrain)

        # current generated mesh and latent codes
        self.geo_z = None
        self.tex_z = None
        self.mesh = None

        # training stuff
        self.training = False
        self.step = 0 # training step 
        self.train_steps = 16

        dpg.create_context()
        self.register_dpg()
        self.test_step()
        

    def __del__(self):
        dpg.destroy_context()


    def train_step(self):

        starter, ender = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        starter.record()

        outputs = self.trainer.train_gui(self.loader, step=self.train_steps)

        ender.record()
        torch.cuda.synchronize()
        t = starter.elapsed_time(ender)

        self.step += self.train_steps
        self.need_update = True

        dpg.set_value("_log_train_time", f'{t:.4f}ms ({int(1000/t)} FPS)')
        dpg.set_value("_log_train_log", f'step = {self.step: 5d} (+{self.train_steps: 2d}), loss = {outputs["loss"]:.4f}, lr = {outputs["lr"]:.5f}')

        # dynamic train steps
        # max allowed train time per-frame is 500 ms
        full_t = t / self.train_steps * 16
        train_steps = min(16, max(4, int(16 * 500 / full_t)))
        if train_steps > self.train_steps * 1.2 or train_steps < self.train_steps * 0.8:
            self.train_steps = train_steps

    
    @torch.no_grad()
    def test_step(self):

        if self.need_update:

            if self.mesh is None:
                return
        
            starter, ender = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            starter.record()

            # do MVP for vertices
            mv = torch.from_numpy(self.cam.view).cuda() # [4, 4]
            proj = torch.from_numpy(self.cam.perspective).cuda() # [4, 4]
            mvp = proj @ mv
            
            v_clip = torch.matmul(F.pad(self.mesh.v, pad=(0, 1), mode='constant', value=1.0),
                              torch.transpose(mvp, 0, 1)).float().unsqueeze(0)  # [1, N, 4]

            rast, rast_db = dr.rasterize(self.glctx, v_clip, self.mesh.f, (self.H, self.W))
            
            if self.mode == 'depth':
                depth = rast[0, :, :, [2]]  # [H, W, 1]
                buffer = depth.detach().cpu().numpy().repeat(3, -1) # [H, W, 3]
            else:
                texc, _ = dr.interpolate(self.mesh.vt.unsqueeze(0).contiguous(), rast, self.mesh.ft)
                albedo = dr.texture(self.mesh.albedo.unsqueeze(0), texc, filter_mode='linear') # [1, H, W, 3]
                albedo = torch.where(rast[..., 3:] > 0, albedo, torch.tensor(0).to(albedo.device)) # remove background
                albedo = dr.antialias(albedo, rast, v_clip, self.mesh.f).clamp(0, 1) # [1, H, W, 3]
                if self.mode == 'albedo':
                    buffer = albedo[0].detach().cpu().numpy()
                else:
                    normal, _ = dr.interpolate(self.mesh.vn.unsqueeze(0).contiguous(), rast, self.mesh.fn)
                    normal = safe_normalize(normal)
                    if self.mode == 'normal':
                        buffer = (normal[0].detach().cpu().numpy() + 1) / 2
                    elif self.mode == 'lambertian':
                        light_d = np.deg2rad(self.light_dir)
                        light_d = np.array([
                            np.sin(light_d[0]) * np.sin(light_d[1]),
                            np.cos(light_d[0]),
                            np.sin(light_d[0]) * np.cos(light_d[1]),
                        ], dtype=np.float32)
                        light_d = torch.from_numpy(light_d).to(albedo.device)
                        lambertian = self.ambient_ratio + (1 - self.ambient_ratio)  * (normal @ light_d).float().clamp(min=0)
                        buffer = (albedo * lambertian.unsqueeze(-1))[0].detach().cpu().numpy()

            ender.record()
            torch.cuda.synchronize()
            t = starter.elapsed_time(ender)

            self.render_buffer = buffer
            self.need_update = False
            
            dpg.set_value("_log_infer_time", f'{t:.4f}ms ({int(1000/t)} FPS)')
            dpg.set_value("_texture", self.render_buffer)

        
    def register_dpg(self):

        ### register texture 

        with dpg.texture_registry(show=False):
            dpg.add_raw_texture(self.W, self.H, self.render_buffer, format=dpg.mvFormat_Float_rgb, tag="_texture")

        ### register window

        # the rendered image, as the primary window
        with dpg.window(tag="_primary_window", width=self.W, height=self.H):

            # add the texture
            dpg.add_image("_texture")

        dpg.set_primary_window("_primary_window", True)

        # control window
        with dpg.window(label="Control", tag="_control_window", width=400, height=300):

            # button theme
            with dpg.theme() as theme_button:
                with dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, (23, 3, 18))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (51, 3, 47))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (83, 18, 83))
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)
                    dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 3, 3)

            # rendering stuff
            with dpg.group(horizontal=True):
                dpg.add_text("Infer time: ")
                dpg.add_text("no data", tag="_log_infer_time")
            
            # mesh stuff
            with dpg.collapsing_header(label="Generate", default_open=True):

                # generate a new mesh
                with dpg.group(horizontal=True):
                    dpg.add_text("GET Mesh: ")

                    def callback_get_mesh(sender, app_data, user_data):
                        _t = time.time()
                        dpg.set_value("_log_get_mesh", f'generating...')
                        self.mesh, self.geo_z, self.tex_z = self.model.generate()
                        self.need_update = True
                        torch.cuda.synchronize()
                        dpg.set_value("_log_get_mesh", f'generated in {time.time() - _t:.4f}s')

                    dpg.add_button(label="get", tag="_button_get_mesh", callback=callback_get_mesh)
                    dpg.bind_item_theme("_button_get_mesh", theme_button)
                    dpg.add_text('', tag="_log_get_mesh")

                # save current mesh
                with dpg.group(horizontal=True):
                    dpg.add_text("Save Mesh: ")

                    def callback_save_mesh(sender, app_data):
                        # TODO: allow set save path
                        self.mesh.write('mesh.obj')
                        dpg.set_value("_log_save_mesh", f'saved mesh!')

                    dpg.add_button(label="save", tag="_button_save_mesh", callback=callback_save_mesh)
                    dpg.bind_item_theme("_button_save_mesh", theme_button)
                    dpg.add_text('', tag="_log_save_mesh")
            
            # train stuff
            with dpg.collapsing_header(label="Drag", default_open=True):
                with dpg.group(horizontal=True):
                    dpg.add_text("Train: ")

                    def callback_train(sender, app_data):
                        if self.training:
                            self.training = False
                            dpg.configure_item("_button_train", label="start")
                        else:
                            self.training = True
                            dpg.configure_item("_button_train", label="stop")

                    dpg.add_button(label="start", tag="_button_train", callback=callback_train)
                    dpg.bind_item_theme("_button_train", theme_button)                   

                with dpg.group(horizontal=True):
                    dpg.add_text("", tag="_log_train_log")

            # rendering options
            with dpg.collapsing_header(label="Options", default_open=True):

                # mode combo
                def callback_change_mode(sender, app_data):
                    self.mode = app_data
                    self.need_update = True
                
                dpg.add_combo(('albedo', 'depth', 'normal', 'lambertian'), label='mode', default_value=self.mode, callback=callback_change_mode)

                # fov slider
                def callback_set_fovy(sender, app_data):
                    self.cam.fovy = app_data
                    self.need_update = True

                dpg.add_slider_int(label="FoV (vertical)", min_value=1, max_value=120, format="%d deg", default_value=self.cam.fovy, callback=callback_set_fovy)

                # light dir
                def callback_set_light_dir(sender, app_data, user_data):
                    self.light_dir[user_data] = app_data
                    self.need_update = True

                dpg.add_separator()
                dpg.add_text("Plane Light Direction:")

                with dpg.group(horizontal=True):
                    dpg.add_slider_float(label="theta", min_value=0, max_value=180, format="%.2f", default_value=self.light_dir[0], callback=callback_set_light_dir, user_data=0)

                with dpg.group(horizontal=True):
                    dpg.add_slider_float(label="phi", min_value=0, max_value=360, format="%.2f", default_value=self.light_dir[1], callback=callback_set_light_dir, user_data=1)

                # ambient ratio
                def callback_set_abm_ratio(sender, app_data):
                    self.ambient_ratio = app_data
                    self.need_update = True

                dpg.add_slider_float(label="ambient", min_value=0, max_value=1.0, format="%.5f", default_value=self.ambient_ratio, callback=callback_set_abm_ratio)

            # debug info
            if self.debug:
                with dpg.collapsing_header(label="Debug"):
                    # pose
                    dpg.add_separator()
                    dpg.add_text("Camera Pose:")
                    dpg.add_text(str(self.cam.pose), tag="_log_pose")


        ### register camera handler

        def callback_camera_drag_rotate(sender, app_data):

            if not dpg.is_item_focused("_primary_window"):
                return

            dx = app_data[1]
            dy = app_data[2]

            self.cam.orbit(dx, dy)
            self.need_update = True

            if self.debug:
                dpg.set_value("_log_pose", str(self.cam.pose))


        def callback_camera_wheel_scale(sender, app_data):

            if not dpg.is_item_focused("_primary_window"):
                return

            delta = app_data

            self.cam.scale(delta)
            self.need_update = True

            if self.debug:
                dpg.set_value("_log_pose", str(self.cam.pose))


        def callback_camera_drag_pan(sender, app_data):

            if not dpg.is_item_focused("_primary_window"):
                return

            dx = app_data[1]
            dy = app_data[2]

            self.cam.pan(dx, dy)
            self.need_update = True

            if self.debug:
                dpg.set_value("_log_pose", str(self.cam.pose))


        with dpg.handler_registry():
            dpg.add_mouse_drag_handler(button=dpg.mvMouseButton_Left, callback=callback_camera_drag_rotate)
            dpg.add_mouse_wheel_handler(callback=callback_camera_wheel_scale)
            dpg.add_mouse_drag_handler(button=dpg.mvMouseButton_Right, callback=callback_camera_drag_pan)

        
        dpg.create_viewport(title='Drag3D', width=self.W, height=self.H, resizable=False)

        ### global theme
        with dpg.theme() as theme_no_padding:
            with dpg.theme_component(dpg.mvAll):
                # set all padding to 0 to avoid scroll bar
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 0, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 0, 0, category=dpg.mvThemeCat_Core)
        
        dpg.bind_item_theme("_primary_window", theme_no_padding)

        dpg.setup_dearpygui()

        #dpg.show_metrics()

        dpg.show_viewport()


    def render(self):

        while dpg.is_dearpygui_running():
            # update texture every frame
            if self.training:
                self.train_step()
            self.test_step()
            dpg.render_dearpygui_frame()


@click.command()
# Required from StyleGAN2.
@click.option('--outdir', help='Where to save the results', metavar='DIR', required=True)
@click.option('--cfg', help='Base configuration', type=click.Choice(['stylegan3-t', 'stylegan3-r', 'stylegan2']), default='stylegan2')
# @click.option('--gpus', help='Number of GPUs to use', metavar='INT', type=click.IntRange(min=1), required=True)
# @click.option('--batch', help='Total batch size', metavar='INT', type=click.IntRange(min=1), required=True)
# @click.option('--gamma', help='R1 regularization weight', metavar='FLOAT', type=click.FloatRange(min=0), required=True)
# My custom configs
### Configs for inference
@click.option('--resume_pretrain', help='Resume from given network pickle', metavar='[PATH|URL]', type=str)
@click.option('--inference_vis', help='whther we run infernce', metavar='BOOL', type=bool, default=False, show_default=True)
@click.option('--inference_to_generate_textured_mesh', help='inference to generate textured meshes', metavar='BOOL', type=bool, default=False, show_default=False)
@click.option('--inference_save_interpolation', help='inference to generate interpolation results', metavar='BOOL', type=bool, default=False, show_default=False)
@click.option('--inference_compute_fid', help='inference to generate interpolation results', metavar='BOOL', type=bool, default=False, show_default=False)
@click.option('--inference_generate_geo', help='inference to generate geometry points', metavar='BOOL', type=bool, default=False, show_default=False)
### Configs for dataset
@click.option('--data', help='Path to the Training data Images', metavar='[DIR]', type=str, default='./tmp')
@click.option('--camera_path', help='Path to the camera root', metavar='[DIR]', type=str, default='./tmp')
@click.option('--img_res', help='The resolution of image', metavar='INT', type=click.IntRange(min=1), default=1024)
@click.option('--data_camera_mode', help='The type of dataset we are using', type=str, default='shapenet_car', show_default=True)
@click.option('--use_shapenet_split', help='whether use the training split or all the data for training', metavar='BOOL', type=bool, default=False, show_default=False)
### Configs for 3D generator##########
@click.option('--use_style_mixing', help='whether use style mixing for generation during inference', metavar='BOOL', type=bool, default=True, show_default=False)
@click.option('--one_3d_generator', help='whether we detach the gradient for empty object', metavar='BOOL', type=bool, default=True, show_default=True)
@click.option('--dmtet_scale', help='Scale for the dimention of dmtet', metavar='FLOAT', type=click.FloatRange(min=0, max=10.0), default=1.0, show_default=True)
@click.option('--n_implicit_layer', help='Number of Implicit FC layer for XYZPlaneTex model', metavar='INT', type=click.IntRange(min=1), default=1)
@click.option('--feat_channel', help='Feature channel for TORGB layer', metavar='INT', type=click.IntRange(min=0), default=16)
@click.option('--mlp_latent_channel', help='mlp_latent_channel for XYZPlaneTex network', metavar='INT', type=click.IntRange(min=8), default=32)
@click.option('--deformation_multiplier', help='Multiplier for the predicted deformation', metavar='FLOAT', type=click.FloatRange(min=1.0), default=1.0, required=False)
@click.option('--tri_plane_resolution', help='The resolution for tri plane', metavar='INT', type=click.IntRange(min=1), default=256)
@click.option('--n_views', help='number of views when training generator', metavar='INT', type=click.IntRange(min=1), default=1)
@click.option('--use_tri_plane', help='Whether use tri plane representation', metavar='BOOL', type=bool, default=True, show_default=True)
@click.option('--tet_res', help='Resolution for teteahedron', metavar='INT', type=click.IntRange(min=1), default=90)
@click.option('--latent_dim', help='Dimention for latent code', metavar='INT', type=click.IntRange(min=1), default=512)
@click.option('--geometry_type', help='The type of geometry generator', type=str, default='conv3d', show_default=True)
@click.option('--render_type', help='Type of renderer we used', metavar='STR', type=click.Choice(['neural_render', 'spherical_gaussian']), default='neural_render', show_default=True)
### Configs for training loss and discriminator#
@click.option('--d_architecture', help='The architecture for discriminator', metavar='STR', type=str, default='skip', show_default=True)
@click.option('--use_pl_length', help='whether we apply path length regularization', metavar='BOOL', type=bool, default=False, show_default=False)  # We didn't use path lenth regularzation to avoid nan error
@click.option('--gamma_mask', help='R1 regularization weight for mask', metavar='FLOAT', type=click.FloatRange(min=0), default=0.0, required=False)
@click.option('--d_reg_interval', help='The internal for R1 regularization', metavar='INT', type=click.IntRange(min=1), default=16)
@click.option('--add_camera_cond', help='Whether we add camera as condition for discriminator', metavar='BOOL', type=bool, default=True, show_default=True)
## Miscs
# Optional features.
@click.option('--cond', help='Train conditional model', metavar='BOOL', type=bool, default=False, show_default=True)
@click.option('--freezed', help='Freeze first layers of D', metavar='INT', type=click.IntRange(min=0), default=0, show_default=True)
# Misc hyperparameters.
@click.option('--batch-gpu', help='Limit batch size per GPU', metavar='INT', type=click.IntRange(min=1), default=4)
@click.option('--cbase', help='Capacity multiplier', metavar='INT', type=click.IntRange(min=1), default=32768, show_default=True)
@click.option('--cmax', help='Max. feature maps', metavar='INT', type=click.IntRange(min=1), default=512, show_default=True)
@click.option('--glr', help='G learning rate  [default: varies]', metavar='FLOAT', type=click.FloatRange(min=0))
@click.option('--dlr', help='D learning rate', metavar='FLOAT', type=click.FloatRange(min=0), default=0.002, show_default=True)
@click.option('--map-depth', help='Mapping network depth  [default: varies]', metavar='INT', type=click.IntRange(min=1))
@click.option('--mbstd-group', help='Minibatch std group size', metavar='INT', type=click.IntRange(min=1), default=4, show_default=True)
# Misc settings.
@click.option('--desc', help='String to include in result dir name', metavar='STR', type=str)
# @click.option('--metrics', help='Quality metrics', metavar='[NAME|A,B,C|none]', type=parse_comma_separated_list, default='fid50k', show_default=True)
@click.option('--kimg', help='Total training duration', metavar='KIMG', type=click.IntRange(min=1), default=20000, show_default=True)
@click.option('--tick', help='How often to print progress', metavar='KIMG', type=click.IntRange(min=1), default=1, show_default=True)  ##
@click.option('--snap', help='How often to save snapshots', metavar='TICKS', type=click.IntRange(min=1), default=50, show_default=True)  ###
@click.option('--seed', help='Random seed', metavar='INT', type=click.IntRange(min=0), default=0, show_default=True)
@click.option('--fp32', help='Disable mixed-precision', metavar='BOOL', type=bool, default=True, show_default=True)  # Let's use fp32 all the case without clamping
@click.option('--nobench', help='Disable cuDNN benchmarking', metavar='BOOL', type=bool, default=False, show_default=True)
@click.option('--workers', help='DataLoader worker processes', metavar='INT', type=click.IntRange(min=0), default=3, show_default=True)
@click.option('-n', '--dry-run', help='Print training options and exit', is_flag=True)
# GUI settings.
@click.option('--height', help='GUI H', metavar='INT', type=click.IntRange(min=1), default=1024)
@click.option('--width', help='GUI W', metavar='INT', type=click.IntRange(min=1), default=1024)
@click.option('--radius', help='GUI radius', metavar='FLOAT', type=click.FloatRange(min=0), default=3)
@click.option('--fovy', help='GUI fovy in degree', metavar='FLOAT', type=click.FloatRange(min=0), default=50)
def main(**kwargs):
    # Initialize config.
    print('==> start')
    opts = dnnlib.EasyDict(kwargs)  # Command line arguments.
    c = dnnlib.EasyDict()  # Main config dict.

    c.H = opts.height
    c.W = opts.width
    c.radius = opts.radius
    c.fovy = opts.fovy

    c.G_kwargs = dnnlib.EasyDict(
        class_name=None, z_dim=opts.latent_dim, w_dim=opts.latent_dim, mapping_kwargs=dnnlib.EasyDict())
    c.D_kwargs = dnnlib.EasyDict(
        class_name='training.networks_get3d.Discriminator', block_kwargs=dnnlib.EasyDict(),
        mapping_kwargs=dnnlib.EasyDict(), epilogue_kwargs=dnnlib.EasyDict())
    c.G_opt_kwargs = dnnlib.EasyDict(class_name='torch.optim.Adam', betas=[0, 0.99], eps=1e-8)
    c.D_opt_kwargs = dnnlib.EasyDict(class_name='torch.optim.Adam', betas=[0, 0.99], eps=1e-8)
    c.loss_kwargs = dnnlib.EasyDict(class_name='training.loss.StyleGAN2Loss')

    c.data_loader_kwargs = dnnlib.EasyDict(pin_memory=True, prefetch_factor=2)
    c.inference_vis = opts.inference_vis
    # Training set.
    if opts.inference_vis:
        c.inference_to_generate_textured_mesh = opts.inference_to_generate_textured_mesh
        c.inference_save_interpolation = opts.inference_save_interpolation
        c.inference_compute_fid = opts.inference_compute_fid
        c.inference_generate_geo = opts.inference_generate_geo

    # c.training_set_kwargs, dataset_name = init_dataset_kwargs(data=opts.data, opt=opts)
    # if opts.cond and not c.training_set_kwargs.use_labels:
    #     raise click.ClickException('--cond=True requires labels specified in dataset.json')
    # c.training_set_kwargs.split = 'train' if opts.use_shapenet_split else 'all'
    # if opts.use_shapenet_split and opts.inference_vis:
    #     c.training_set_kwargs.split = 'test'
    # c.training_set_kwargs.use_labels = opts.cond
    # c.training_set_kwargs.xflip = False
    # Hyperparameters & settings.p
    c.G_kwargs.one_3d_generator = opts.one_3d_generator
    c.G_kwargs.n_implicit_layer = opts.n_implicit_layer
    c.G_kwargs.deformation_multiplier = opts.deformation_multiplier
    c.resume_pretrain = opts.resume_pretrain
    c.D_reg_interval = opts.d_reg_interval
    c.G_kwargs.use_style_mixing = opts.use_style_mixing
    c.G_kwargs.dmtet_scale = opts.dmtet_scale
    c.G_kwargs.feat_channel = opts.feat_channel
    c.G_kwargs.mlp_latent_channel = opts.mlp_latent_channel
    c.G_kwargs.tri_plane_resolution = opts.tri_plane_resolution
    c.G_kwargs.n_views = opts.n_views

    c.G_kwargs.render_type = opts.render_type
    c.G_kwargs.use_tri_plane = opts.use_tri_plane
    c.D_kwargs.data_camera_mode = opts.data_camera_mode
    c.D_kwargs.add_camera_cond = opts.add_camera_cond

    c.G_kwargs.tet_res = opts.tet_res

    c.G_kwargs.geometry_type = opts.geometry_type
    # c.num_gpus = opts.gpus
    # c.batch_size = opts.batch
    # c.batch_gpu = opts.batch_gpu or opts.batch // opts.gpus
    # c.G_kwargs.geo_pos_enc = opts.geo_pos_enc
    c.G_kwargs.data_camera_mode = opts.data_camera_mode
    c.G_kwargs.channel_base = c.D_kwargs.channel_base = opts.cbase
    c.G_kwargs.channel_max = c.D_kwargs.channel_max = opts.cmax

    c.G_kwargs.mapping_kwargs.num_layers = 8

    c.D_kwargs.architecture = opts.d_architecture
    c.D_kwargs.block_kwargs.freeze_layers = opts.freezed
    c.D_kwargs.epilogue_kwargs.mbstd_group_size = opts.mbstd_group
    # c.loss_kwargs.gamma_mask = opts.gamma if opts.gamma_mask == 0.0 else opts.gamma_mask
    # c.loss_kwargs.r1_gamma = opts.gamma
    c.G_opt_kwargs.lr = (0.002 if opts.cfg == 'stylegan2' else 0.0025) if opts.glr is None else opts.glr
    c.D_opt_kwargs.lr = opts.dlr
    # c.metrics = opts.metrics
    c.total_kimg = opts.kimg
    c.kimg_per_tick = opts.tick
    c.image_snapshot_ticks = c.network_snapshot_ticks = opts.snap
    # c.random_seed = c.training_set_kwargs.random_seed = opts.seed
    c.random_seed = opts.seed
    c.data_loader_kwargs.num_workers = opts.workers
    c.network_snapshot_ticks = 200
    # Sanity checks.
    # if c.batch_size % c.num_gpus != 0:
    #     raise click.ClickException('--batch must be a multiple of --gpus')
    # if c.batch_size % (c.num_gpus * c.batch_gpu) != 0:
    #     raise click.ClickException('--batch must be a multiple of --gpus times --batch-gpu')
    # if c.batch_gpu < c.D_kwargs.epilogue_kwargs.mbstd_group_size:
    #     raise click.ClickException('--batch-gpu cannot be smaller than --mbstd')
    # if any(not metric_main.is_valid_metric(metric) for metric in c.metrics):
    #     raise click.ClickException(
    #         '\n'.join(['--metrics can only contain the following values:'] + metric_main.list_valid_metrics()))

    # Base configuration.
    # c.ema_kimg = c.batch_size * 10 / 32
    c.G_kwargs.class_name = 'training.networks_get3d.GeneratorDMTETMesh'
    c.loss_kwargs.style_mixing_prob = 0.9  # Enable style mixing regularization.
    c.loss_kwargs.pl_weight = 0.0  # Enable path length regularization.
    c.G_reg_interval = 4  # Enable lazy regularization for G.
    c.G_kwargs.fused_modconv_default = 'inference_only'  # Speed up training by using regular convolutions instead of grouped convolutions.
    # Performance-related toggles.
    if opts.fp32:
        c.G_kwargs.num_fp16_res = c.D_kwargs.num_fp16_res = 0
        c.G_kwargs.conv_clamp = c.D_kwargs.conv_clamp = None
    if opts.nobench:
        c.cudnn_benchmark = False

    # launch gui
    gui = GUI(c)
    gui.render()
    

if __name__ == "__main__":
    main()