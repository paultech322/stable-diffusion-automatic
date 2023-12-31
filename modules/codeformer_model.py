import os
import sys
import traceback

import cv2
import torch

from modules import shared, devices
from modules.paths import script_path
import modules.shared
import modules.face_restoration
from importlib import reload

# codeformer people made a choice to include modified basicsr librry to their projectwhich makes
# it utterly impossiblr to use it alongside with other libraries that also use basicsr, like GFPGAN.
# I am making a choice to include some files from codeformer to work around this issue.

pretrain_model_url = 'https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/codeformer.pth'

have_codeformer = False
codeformer = None

def setup_codeformer():
    path = modules.paths.paths.get("CodeFormer", None)
    if path is None:
        return


    # both GFPGAN and CodeFormer use bascisr, one has it installed from pip the other uses its own
    #stored_sys_path = sys.path
    #sys.path = [path] + sys.path

    try:
        from torchvision.transforms.functional import normalize
        from modules.codeformer.codeformer_arch import CodeFormer
        from basicsr.utils.download_util import load_file_from_url
        from basicsr.utils import imwrite, img2tensor, tensor2img
        from facelib.utils.face_restoration_helper import FaceRestoreHelper
        from modules.shared import cmd_opts

        net_class = CodeFormer

        class FaceRestorerCodeFormer(modules.face_restoration.FaceRestoration):
            def name(self):
                return "CodeFormer"

            def __init__(self):
                self.net = None
                self.face_helper = None
                if shared.device.type == 'mps': # CodeFormer currently does not support mps backend
                    shared.device_codeformer = torch.device('cpu')

            def create_models(self):

                if self.net is not None and self.face_helper is not None:
                    self.net.to(shared.device)
                    return self.net, self.face_helper

                net = net_class(dim_embd=512, codebook_size=1024, n_head=8, n_layers=9, connect_list=['32', '64', '128', '256']).to(devices.device_codeformer)
                ckpt_path = load_file_from_url(url=pretrain_model_url, model_dir=os.path.join(path, 'weights/CodeFormer'), progress=True)
                checkpoint = torch.load(ckpt_path)['params_ema']
                net.load_state_dict(checkpoint)
                net.eval()

                face_helper = FaceRestoreHelper(1, face_size=512, crop_ratio=(1, 1), det_model='retinaface_resnet50', save_ext='png', use_parse=True, device=devices.device_codeformer)

                self.net = net
                self.face_helper = face_helper
                self.net.to(shared.device)

                return net, face_helper

            def restore(self, np_image, w=None):
                np_image = np_image[:, :, ::-1]

                original_resolution = np_image.shape[0:2]

                self.create_models()
                self.face_helper.clean_all()
                self.face_helper.read_image(np_image)
                self.face_helper.get_face_landmarks_5(only_center_face=False, resize=640, eye_dist_threshold=5)
                self.face_helper.align_warp_face()

                for idx, cropped_face in enumerate(self.face_helper.cropped_faces):
                    cropped_face_t = img2tensor(cropped_face / 255., bgr2rgb=True, float32=True)
                    normalize(cropped_face_t, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
                    cropped_face_t = cropped_face_t.unsqueeze(0).to(devices.device_codeformer)

                    try:
                        with torch.no_grad():
                            output = self.net(cropped_face_t, w=w if w is not None else shared.opts.code_former_weight, adain=True)[0]
                            restored_face = tensor2img(output, rgb2bgr=True, min_max=(-1, 1))
                        del output
                        torch.cuda.empty_cache()
                    except Exception as error:
                        print(f'\tFailed inference for CodeFormer: {error}', file=sys.stderr)
                        restored_face = tensor2img(cropped_face_t, rgb2bgr=True, min_max=(-1, 1))

                    restored_face = restored_face.astype('uint8')
                    self.face_helper.add_restored_face(restored_face)

                self.face_helper.get_inverse_affine(None)

                restored_img = self.face_helper.paste_faces_to_input_image()
                restored_img = restored_img[:, :, ::-1]

                if original_resolution != restored_img.shape[0:2]:
                    restored_img = cv2.resize(restored_img, (0, 0), fx=original_resolution[1]/restored_img.shape[1], fy=original_resolution[0]/restored_img.shape[0], interpolation=cv2.INTER_LINEAR)

                if shared.opts.face_restoration_unload:
                    self.net.to(devices.cpu)

                return restored_img

        global have_codeformer
        have_codeformer = True

        global codeformer
        codeformer = FaceRestorerCodeFormer()
        shared.face_restorers.append(codeformer)

    except Exception:
        print("Error setting up CodeFormer:", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)

   # sys.path = stored_sys_path
