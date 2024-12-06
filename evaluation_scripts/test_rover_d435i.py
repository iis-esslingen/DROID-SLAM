import sys
sys.path.append('droid_slam')

from tqdm import tqdm
import numpy as np
import torch
import lietorch
import cv2
import os
import glob 
import time
import argparse

import torch.nn.functional as F
from droid import Droid

import matplotlib.pyplot as plt


def show_image(image):
    image = image.permute(1, 2, 0).cpu().numpy()
    cv2.imshow('image', image / 255.0)
    cv2.waitKey(1)

def image_stream(datapath, use_depth=False, stride=1):
    """ image generator """

    fx, fy, cx, cy = 596.1950425915236, 593.1411835433107, 327.0463454538411, 245.16142133264628
    K_l = np.array([fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0]).reshape(3,3)
    d_l = np.array([0.07561607662577634, -0.2088729975389971, -0.0023347441072184756, 0.004010356017731083])

    image_list = sorted(glob.glob(os.path.join(datapath, 'rgb', '*.png')))[::stride]
    depth_list = sorted(glob.glob(os.path.join(datapath, 'depth', '*.png')))[::stride]

    for t, (image_file, depth_file) in enumerate(zip(image_list, depth_list)):

        image = cv2.imread(image_file)
        image = cv2.undistort(image, K_l, d_l)
        depth = cv2.imread(depth_file, cv2.IMREAD_ANYDEPTH) / 1000.0

        h0, w0, _ = image.shape
        h1 = int(h0 * np.sqrt((384 * 512) / (h0 * w0)))
        w1 = int(w0 * np.sqrt((384 * 512) / (h0 * w0)))

        image = cv2.resize(image, (w1, h1))
        # image = image[:h1-h1%8, :w1-w1%8]
        image = torch.as_tensor(image).permute(2, 0, 1)
        
        depth = torch.as_tensor(depth)
        depth = F.interpolate(depth[None,None], (h1, w1)).squeeze()
        # depth = depth[:h1-h1%8, :w1-w1%8]

        intrinsics = torch.as_tensor([fx, fy, cx, cy])
        intrinsics[0::2] *= (w1 / w0)
        intrinsics[1::2] *= (h1 / h0)

        if use_depth:
            yield t, image[None], depth, intrinsics

        else:
            yield t, image[None], None, intrinsics

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--datapath")
    parser.add_argument("--outputpath")
    
    parser.add_argument("--weights", default="droid.pth")
    parser.add_argument("--buffer", type=int, default=4096)
    parser.add_argument("--image_size", default=[480, 640])
    parser.add_argument("--disable_vis", action="store_true")
    parser.add_argument("--stride", type=int, default=2)

    parser.add_argument("--beta", type=float, default=0.5)
    parser.add_argument("--filter_thresh", type=float, default=2.0)
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--keyframe_thresh", type=float, default=3.5)
    parser.add_argument("--frontend_thresh", type=float, default=16.0)
    parser.add_argument("--frontend_window", type=int, default=16)
    parser.add_argument("--frontend_radius", type=int, default=1)
    parser.add_argument("--frontend_nms", type=int, default=0)

    parser.add_argument("--depth", action="store_true")
    parser.add_argument("--stereo", action="store_true")

    parser.add_argument("--backend_thresh", type=float, default=22.0)
    parser.add_argument("--backend_radius", type=int, default=2)
    parser.add_argument("--backend_nms", type=int, default=3)
    parser.add_argument("--upsample", action="store_true")
    args = parser.parse_args()

    torch.multiprocessing.set_start_method('spawn')

    print("Running evaluation on {}".format(args.datapath))
    print(args)

    # this can usually be set to 2-3 except for "camera_shake" scenes
    # set to 2 for test scenes
    stride = args.stride

    tstamps = []
    for (t, image, depth, intrinsics) in tqdm(image_stream(args.datapath, use_depth=args.depth, stride=stride)):
        if not args.disable_vis:
            show_image(image[0])

        if t == 0:
            args.image_size = [image.shape[2], image.shape[3]]
            droid = Droid(args)
        
        droid.track(t, image, depth, intrinsics=intrinsics)
    
    traj_est = droid.terminate(image_stream(args.datapath, use_depth=args.depth, stride=stride))
    
    ### run evaluation ###

    print("#"*20 + " Results...")

    import evo
    from evo.core.trajectory import PoseTrajectory3D
    from evo.tools import file_interface
    from evo.core import sync
    import evo.main_ape as main_ape
    import evo.main_rpe as main_rpe
    from evo.core.metrics import PoseRelation
    from pathlib import Path
    import json

    image_path = os.path.join(args.datapath, 'rgb')
    images_list = sorted(glob.glob(os.path.join(image_path, '*.png')))[::stride]
    tstamps = [float(x.split('/')[-1][:-4]) for x in images_list]

    traj_est_fused = PoseTrajectory3D(
        positions_xyz=traj_est[:,:3],
        orientations_quat_wxyz=traj_est[:,3:],
        timestamps=np.array(tstamps))

    result_path = os.path.join(args.outputpath)
    Path(result_path).mkdir(parents=True, exist_ok=True)
    result_file_name = "d435i_rgbd_slam_trajectory.txt" if args.depth else "d435i_mono_slam_trajectory.txt"
    file_interface.write_tum_trajectory_file(os.path.join(result_path, result_file_name), traj_est_fused)
    
    gt_file = os.path.join(args.datapath, 'groundtruth.txt')
    traj_ref = file_interface.read_tum_trajectory_file(gt_file)

    traj_ref, traj_est = sync.associate_trajectories(traj_ref, traj_est_fused)

    result_ape = main_ape.ape(traj_ref, traj_est, est_name='traj', 
        pose_relation=PoseRelation.translation_part, align=True, correct_scale=False if args.depth else True)

    with open(os.path.join(result_path, result_file_name.replace("slam_trajectory", "ape_results")), "w") as file:
        file.write(json.dumps(result_ape.stats))

    # result_rpe = main_rpe.rpe(traj_ref, traj_est, est_name='traj', 
    #     pose_relation=PoseRelation.translation_part, align=True, correct_scale=False if args.depth else True)

    # with open(os.path.join(result_path, result_file_name.replace("slam_trajectory", "rpe_results")), "w") as file:
    #     file.write(json.dumps(result_rpe.stats))