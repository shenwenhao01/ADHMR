import os
import os.path as osp
import numpy as np
import torch
import cv2
import json
import copy
from pycocotools.coco import COCO
from config import cfg
from utils.human_models import smpl_x, smpl
from utils.preprocessing import load_img, process_bbox, augmentation, process_human_model_output, process_db_coord
from utils.transforms import rigid_align
import numpy as np
import random
from mmhuman3d.core.conventions.keypoints_mapping import convert_kps


class PW3D(torch.utils.data.Dataset):
    def __init__(self, transform, data_split):
        self.transform = transform
        self.data_split = data_split
        self.data_path = osp.join(cfg.data_dir, 'PW3D', 'data')
        # 3dpw skeleton
        self.joint_set = {
            'joint_num': smpl_x.joint_num,
            'joints_name': smpl_x.joints_name,
            'flip_pairs': smpl_x.flip_pairs}
        self.datalist = self.load_data()

    def load_data(self):
        db = COCO(osp.join(self.data_path, '3DPW_' + self.data_split + '.json'))

        datalist = []
        i = 0
        if getattr(cfg, 'eval_on_train', False):
            self.data_split = 'eval_train'
            print("Evaluate on train set.")

        for aid in db.anns.keys():
            i += 1
            if 'train' in self.data_split and i % getattr(cfg, 'PW3D_train_sample_interval', 1) != 0:
                continue

            ann = db.anns[aid]
            image_id = ann['image_id']
            img = db.loadImgs(image_id)[0]
            sequence_name = img['sequence']
            img_name = img['file_name']
            img_path = osp.join(self.data_path, 'imageFiles', sequence_name, img_name)
            cam_param = {k: np.array(v, dtype=np.float32) for k,v in img['cam_param'].items()}

            smpl_param = ann['smpl_param']
            bbox = process_bbox(np.array(ann['bbox']), img['width'], img['height'], ratio=getattr(cfg, 'bbox_ratio', 1.25))
            if bbox is None: continue
            data_dict = {'img_path': img_path, 
                         'ann_id': aid, 
                         'img_shape': (img['height'], img['width']), 
                         'bbox': bbox, 
                         'smpl_param': smpl_param, 
                         'cam_param': cam_param,
                         'focal': cam_param['focal'],
                         'princpt': cam_param['princpt'],
                        }
            datalist.append(data_dict)

        if self.data_split == 'train':
            print('[PW3D train] original size:', len(db.anns.keys()),
                  '. Sample interval:', getattr(cfg, 'PW3D_train_sample_interval', 1),
                  '. Sampled size:', len(datalist))
        
        if (getattr(cfg, 'data_strategy', None) == 'balance' and self.data_split == 'train') or \
                self.data_split == 'eval_train':
            print(f'[PW3D] Using [balance] strategy with datalist shuffled...')
            random.seed(2023)
            random.shuffle(datalist)
            
            if self.data_split == "eval_train":
                return datalist[:10000]

        return datalist

    def __len__(self):
        return len(self.datalist)

    def __getitem__(self, idx):
        data = copy.deepcopy(self.datalist[idx])
        img_path, img_shape = data['img_path'], data['img_shape']
        
        # img
        img = load_img(img_path)
        bbox, smpl_param, cam_param = data['bbox'], data['smpl_param'], data['cam_param']
        img, img2bb_trans, bb2img_trans, rot, do_flip, focal_scale = augmentation(img, bbox, self.data_split)
        img = self.transform(img.astype(np.float32)) / 255.
        cam_param = data['cam_param']

        if self.data_split == 'train':

            # Process focal and princpt
            focal = np.array(cam_param['focal']).copy() if 'focal' in cam_param else None
            princpt = np.array(cam_param['princpt']).copy() if 'princpt' in cam_param else None
            if princpt.size == 1:
                princpt = princpt.repeat(2)
            
            if focal is not None and princpt is not None:
                focal = focal * focal_scale
            rot_aug_mat = np.array([[np.cos(np.deg2rad(-rot)), -np.sin(np.deg2rad(-rot)), 0],
                                [np.sin(np.deg2rad(-rot)), np.cos(np.deg2rad(-rot)), 0],
                                [0, 0, 1]], dtype=np.float32)
            smpl_cam_trans = np.array(smpl_param['trans']).copy()
            if do_flip:
                smpl_cam_trans[0] = -smpl_cam_trans[0]
                princpt[0] = img_shape[1] - 1 - princpt[0]
            princpt_xy1 = np.concatenate((princpt[:2], np.ones_like(princpt[:1])), 0)
            princpt[:2] = np.dot(img2bb_trans, princpt_xy1)
        

            smplx_param = {}
            smplx_param['root_pose'] = np.array(smpl_param['pose']).reshape(-1,3)[:1, :]
            smplx_param['body_pose'] = np.array(smpl_param['pose']).reshape(-1,3)[1:22, :]
            smplx_param['trans'] = np.array(smpl_param['trans']).reshape(-1,3)
            smplx_param['shape'] = np.zeros(10, dtype=np.float32) # drop smpl betas for smplx


            # smpl coordinates
            smplx_joint_img, smplx_joint_cam, smplx_joint_trunc, smplx_pose, smplx_shape, smplx_expr, \
                smplx_pose_valid, smplx_joint_valid, smplx_expr_valid, smplx_mesh_cam_orig = process_human_model_output(
                    smplx_param, cam_param, do_flip, img_shape, img2bb_trans, rot, 'smplx',
                    joint_img=None)
            
            # reverse ra
            smplx_joint_cam_wo_ra = smplx_joint_cam.copy()
            smplx_joint_cam_wo_ra[smpl_x.joint_part['lhand'], :] = smplx_joint_cam_wo_ra[smpl_x.joint_part['lhand'], :] \
                                                            + smplx_joint_cam_wo_ra[smpl_x.lwrist_idx, None, :]  # left hand root-relative
            smplx_joint_cam_wo_ra[smpl_x.joint_part['rhand'], :] = smplx_joint_cam_wo_ra[smpl_x.joint_part['rhand'], :] \
                                                            + smplx_joint_cam_wo_ra[smpl_x.rwrist_idx, None, :]  # right hand root-relative
            smplx_joint_cam_wo_ra[smpl_x.joint_part['face'], :] = smplx_joint_cam_wo_ra[smpl_x.joint_part['face'], :] \
                                                            + smplx_joint_cam_wo_ra[smpl_x.neck_idx, None,: ]  # face root-relative


        
            smplx_pose_valid = np.tile(smplx_pose_valid[:, None], (1, 3)).reshape(-1)
            smplx_joint_valid = smplx_joint_valid[:, None]
            smplx_joint_trunc = smplx_joint_valid * smplx_joint_trunc

            # smpl coordinates
            smpl_joint_img, _, _, _, _, _ = process_human_model_output(
                    smpl_param, cam_param, do_flip, img_shape, img2bb_trans, rot, 'smpl',
                    joint_img=None)

            joint_img = np.zeros_like(smplx_joint_img)
            joint_img[:22] = smpl_joint_img[:22, :]

            if False:                            # vis joint proj
                from mmhuman3d.core.conventions.keypoints_mapping import convert_kps

                # smplx_joint_29, _ = convert_kps(smplx_joint_img, src='smplx', dst='hybrik_29')
                vis_smplx_joint_img = smplx_joint_img.copy()
                vis_smpl_joint_img = smpl_joint_img.copy()
                # get image
                image = img.cpu().numpy().copy().transpose(1, 2, 0) * 255
                image = image.astype(np.uint8).copy() 
                image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

                color = [(0, 0, 255), (0, 255, 0)]      # Red: smplx; Green: smpl
                for set_id, joint_proj_ in enumerate([vis_smplx_joint_img, vis_smpl_joint_img]):
                    th = 2
                    joint_proj_[:, 0] = joint_proj_[:, 0] / cfg.output_hm_shape[2] * cfg.input_img_shape[1]
                    joint_proj_[:, 1] = joint_proj_[:, 1] / cfg.output_hm_shape[1] * cfg.input_img_shape[0]
                    n_kps = joint_proj_.shape[0]

                    for i in range (n_kps):
                        kps = joint_proj_[i]
                        image = cv2.circle(image, (int(kps[0]),int(kps[1])), radius=th, color=color[set_id], thickness=th)
                
                imgname = f'./debug.png'
                cv2.imwrite(imgname, image)
                print(imgname)

            # dummy hand/face bbox
            dummy_center = np.zeros((2), dtype=np.float32)
            dummy_size = np.zeros((2), dtype=np.float32)


            inputs = {'img': img}
            targets = {'joint_img': joint_img, 'smplx_joint_img': joint_img, 
                        'joint_cam': smplx_joint_cam_wo_ra, 'smplx_joint_cam': smplx_joint_cam, 
                        'smplx_pose': smplx_pose, 'smplx_shape': smplx_shape, 'smplx_expr': smplx_expr, 
                        'lhand_bbox_center': dummy_center, 'lhand_bbox_size': dummy_size, 
                        'rhand_bbox_center': dummy_center, 'rhand_bbox_size': dummy_size, 
                        'face_bbox_center': dummy_center, 'face_bbox_size': dummy_size,
                        #  For reproj
                       'smplx_cam_trans' : smpl_cam_trans.astype(np.float32),}
            meta_info = {'joint_valid': smplx_joint_valid, 'joint_trunc': smplx_joint_trunc, 
                            'smplx_joint_valid': smplx_joint_valid, 'smplx_joint_trunc': smplx_joint_trunc, 
                            'smplx_pose_valid': smplx_pose_valid, 'smplx_shape_valid': float(False), 
                            'smplx_expr_valid': float(smplx_expr_valid), 'is_3D': float(True), 
                            'lhand_bbox_valid': float(False), 'rhand_bbox_valid': float(False), 
                            'face_bbox_valid': float(False),
                            #  For reproj
                            'focal': focal,
                            'princpt': princpt,
                            'rot_aug_mat': rot_aug_mat,}
            return inputs, targets, meta_info
    
        else:

            # smpl coordinates
            smpl_joint_img, smpl_joint_cam, smpl_joint_trunc, smpl_pose, smpl_shape, smpl_mesh_cam_orig = process_human_model_output(smpl_param, cam_param, do_flip, img_shape, img2bb_trans, rot, 'smpl')

            inputs = {'img': img}
            targets = {'smpl_mesh_cam': smpl_mesh_cam_orig}
            meta_info = {}
            return inputs, targets, meta_info

    
    def evaluate(self, outs, cur_sample_idx):
        annots = self.datalist
        sample_num = len(outs)
        eval_result = {'mpjpe_body': [], 'pa_mpjpe_body': [], }
        
        ## smpl/smplx -> lsp
        #     ['left_hip', 'right_hip', 'left_knee', 'right_knee', 'left_ankle',
        #    'right_ankle', 'neck', 'head', 'left_shoulder', 'right_shoulder',
        #    'left_elbow', 'right_elbow', 'left_wrist', 'right_wrist']
        joint_mapper = [1, 2, 4, 5, 7, 8, 12, 15, 16, 17, 18, 19, 20, 21]

        
        for n in range(sample_num):

            out = outs[n]

            # MPVPE from all vertices
            mesh_gt = out['smpl_mesh_cam_target']
            mesh_out = out['smplx_mesh_cam']

            # MPJPE from body joints
            mesh_out_align = mesh_out - np.dot(smpl_x.J_regressor, mesh_out)[smpl_x.J_regressor_idx['pelvis'], None, :] \
                                      + np.dot(smpl.joint_regressor, mesh_gt)[smpl.root_joint_idx, None, :]

            # only test 14 keypoints
            joint_gt_body = np.dot(smpl.joint_regressor, mesh_gt)[joint_mapper, :] 
            joint_out_body = np.dot(smpl_x.J_regressor, mesh_out)[joint_mapper, :] 
            joint_out_body_root_align = np.dot(smpl_x.J_regressor, mesh_out_align)[joint_mapper, :]

            eval_result['mpjpe_body'].append(
                np.sqrt(np.sum((joint_out_body_root_align - joint_gt_body) ** 2, 1)).mean() * 1000)

            # PAMPJPE from body joints
            joint_out_body_pa_align = rigid_align(joint_out_body, joint_gt_body)
            eval_result['pa_mpjpe_body'].append(
                np.sqrt(np.sum((joint_out_body_pa_align - joint_gt_body) ** 2, 1)).mean() * 1000)

        return eval_result

    def print_eval_result(self, eval_result):
        print('======3DPW-test======')
        print('MPJPE (Body): %.2f mm' % np.mean(eval_result['mpjpe_body']))
        print('PA MPJPE (Body): %.2f mm' % np.mean(eval_result['pa_mpjpe_body']))
        print()
        print(f"{np.mean(eval_result['mpjpe_body'])},{np.mean(eval_result['pa_mpjpe_body'])}")
        print()

        f = open(os.path.join(cfg.result_dir, 'result.txt'), 'w')
        f.write(f'3DPW-test dataset: \n')
        f.write('MPJPE (Body): %.2f mm\n' % np.mean(eval_result['mpjpe_body']))
        f.write('PA MPJPE (Body): %.2f mm\n' % np.mean(eval_result['pa_mpjpe_body']))

        f.write(f"{np.mean(eval_result['mpjpe_body'])},{np.mean(eval_result['pa_mpjpe_body'])}")

        if getattr(cfg, 'eval_on_train', False):
            import csv
            csv_file = f'{cfg.root_dir}/output/pw3d_eval_on_train.csv'
            exp_id = cfg.exp_name.split('_')[1]
            new_line = [exp_id,np.mean(eval_result['mpjpe_body']), np.mean(eval_result['pa_mpjpe_body'])]

            # Append the new line to the CSV file
            with open(csv_file, 'a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(new_line)


class PW3D_Clean(torch.utils.data.Dataset):
    def __init__(self, transform, data_split):
        self.transform = transform
        self.data_split = 'train'               # score 3DPW training set
        self.data_path = osp.join(cfg.data_dir, 'PW3D', 'data')
        # 3dpw skeleton
        self.joint_set = {
            'joint_num': smpl_x.joint_num,
            'joints_name': smpl_x.joints_name,
            'flip_pairs': smpl_x.flip_pairs}
        self.datalist = self.load_data()

    def load_data(self):
        db = COCO(osp.join(self.data_path, '3DPW_' + self.data_split + '.json'))

        datalist = []
        i = 0
        if getattr(cfg, 'eval_on_train', False):
            self.data_split = 'eval_train'
            print("Evaluate on train set.")

        for aid in db.anns.keys():
            i += 1
            if 'train' in self.data_split and i % getattr(cfg, 'PW3D_train_sample_interval', 1) != 0:
                continue

            ann = db.anns[aid]
            image_id = ann['image_id']
            img = db.loadImgs(image_id)[0]
            sequence_name = img['sequence']
            img_name = img['file_name']
            img_path = osp.join(self.data_path, 'imageFiles', sequence_name, img_name)
            cam_param = {k: np.array(v, dtype=np.float32) for k,v in img['cam_param'].items()}

            smpl_param = ann['smpl_param']
            bbox = process_bbox(np.array(ann['bbox']), img['width'], img['height'], ratio=getattr(cfg, 'bbox_ratio', 1.25))
            if bbox is None: continue
            data_dict = {'img_path': img_path, 
                         'ann_id': aid, 
                         'img_shape': (img['height'], img['width']), 
                         'bbox': bbox, 
                         'smpl_param': smpl_param, 
                         'cam_param': cam_param,
                         'focal': cam_param['focal'],
                         'princpt': cam_param['princpt'],
                        }
            datalist.append(data_dict)

        if self.data_split == 'train':
            print('[PW3D train] original size:', len(db.anns.keys()),
                  '. Sample interval:', getattr(cfg, 'PW3D_train_sample_interval', 1),
                  '. Sampled size:', len(datalist))
        
        if (getattr(cfg, 'data_strategy', None) == 'balance' and self.data_split == 'train') or \
                self.data_split == 'eval_train':
            print(f'[PW3D] Using [balance] strategy with datalist NOT shuffled...')
            
            if self.data_split == "eval_train":
                return datalist[:10000]

        return datalist

    def __len__(self):
        return len(self.datalist)

    def __getitem__(self, idx):
        data = copy.deepcopy(self.datalist[idx])
        img_path, img_shape = data['img_path'], data['img_shape']
        
        # img
        img = load_img(img_path)
        bbox, smpl_param, cam_param = data['bbox'], data['smpl_param'], data['cam_param']
        img, img2bb_trans, bb2img_trans, rot, do_flip, focal_scale = augmentation(img, bbox, 'test')
        img = self.transform(img.astype(np.float32)) / 255.
        cam_param = data['cam_param']


        # smpl coordinates
        smpl_joint_img, smpl_joint_cam, smpl_joint_trunc, smpl_pose, smpl_shape, smpl_mesh_cam_orig = process_human_model_output(
            smpl_param, cam_param, do_flip, img_shape, img2bb_trans, rot, 'smpl')
        
        smpl_joint_img_29, _ = convert_kps(smpl_joint_img[np.newaxis], src='smpl', dst='hybrik_29')
        smpl_joint_img_29 = smpl_joint_img_29[0]
        x_29_norm = (smpl_joint_img_29[:, 0, np.newaxis] / cfg.output_hm_shape[2] - 0.5) * 2.
        y_29_norm = (smpl_joint_img_29[:, 1, np.newaxis] / cfg.output_hm_shape[1] - 0.5) * 2.

        smpl_joint_cam_29, _ = convert_kps(smpl_joint_cam[np.newaxis], src='smpl', dst='hybrik_29')
        smpl_joint_cam_29 = smpl_joint_cam_29[0] - smpl_joint_cam_29[0, 0]
        d_29 = smpl_joint_cam_29[:, 2, np.newaxis]

        score_joints = np.concatenate([x_29_norm, y_29_norm, d_29], axis=1).astype(np.float32)
        

        if True:                            # vis joint proj
            vis_joint_img_1 = smpl_joint_img.copy()
            vis_joint_img_2 = smpl_joint_img_29.copy()
            # get image
            image = img.cpu().numpy().copy().transpose(1, 2, 0) * 255
            image = image.astype(np.uint8).copy() 
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

            vis_joint_img_1[:, 0] = vis_joint_img_1[:, 0] / cfg.output_hm_shape[2] * cfg.input_img_shape[1]
            vis_joint_img_1[:, 1] = vis_joint_img_1[:, 1] / cfg.output_hm_shape[1] * cfg.input_img_shape[0]
            vis_joint_img_2[:, 0] = vis_joint_img_2[:, 0] / cfg.output_hm_shape[2] * cfg.input_img_shape[1]
            vis_joint_img_2[:, 1] = vis_joint_img_2[:, 1] / cfg.output_hm_shape[1] * cfg.input_img_shape[0]

            color = [(0, 0, 255), (0, 255, 0)]      # Red: smplx; Green: smpl
            th = 2
            for set_id, joint_proj_ in enumerate([vis_joint_img_1, vis_joint_img_2]):
                n_kps = joint_proj_.shape[0]

                for i in range (n_kps):
                    kps = joint_proj_[i]
                    image = cv2.circle(image, (int(kps[0]),int(kps[1])), radius=th, color=color[set_id], thickness=th)
            
            imgname = f'./debug.png'
            cv2.imwrite(imgname, image)
            print(imgname)
            import ipdb; ipdb.set_trace()


        inputs = {'img': img}
        targets = {}
        meta_info = {'img_path': img_path, 
                     'ann_id': data['ann_id']}
        gen_output = {'score_joints': score_joints}
        return inputs, targets, meta_info, gen_output
