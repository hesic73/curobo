from curobo.geom.sdf.world import CollisionCheckerType
from curobo.geom.types import WorldConfig
from fr3_algr_zed2i_world import get_table_collision_dict, get_object_collision_dict
import transforms3d
import pathlib
from typing import Optional, Tuple
from curobo.types.base import TensorDeviceType
from curobo.types.math import Pose
from curobo.types.robot import JointState, RobotConfig
import torch
import numpy as np
import time
from curobo.util_file import (
    get_robot_configs_path,
    join_path,
    load_yaml,
)
from curobo.wrap.reacher.motion_gen import (
    MotionGen,
    MotionGenConfig,
    MotionGenPlanConfig,
    MotionGenResult,
)


DEFAULT_Q_FR3 = np.array(
    [
        1.76261055e-06,
        -1.29018439e00,
        0.00000000e00,
        -2.69272642e00,
        0.00000000e00,
        1.35254201e00,
        7.85400000e-01,
    ]
)
DEFAULT_Q_ALGR = np.array(
    [
        2.90945620e-01,
        7.37109400e-01,
        5.10859200e-01,
        1.22637060e-01,
        1.20125350e-01,
        5.84513500e-01,
        3.43829930e-01,
        6.05035000e-01,
        -2.68431900e-01,
        8.78457900e-01,
        8.49713500e-01,
        8.97218400e-01,
        1.33282830e00,
        3.47787830e-01,
        2.09215670e-01,
        -6.50969000e-03,
    ]
)
DEFAULT_Q = np.concatenate([DEFAULT_Q_FR3, DEFAULT_Q_ALGR])


def solve_trajopt(
    X_W_H: np.ndarray,
    q_algr_constraint: Optional[np.ndarray] = None,
    q_fr3_start: Optional[np.ndarray] = None,
    collision_check_object: bool = True,
    obj_filepath: Optional[pathlib.Path] = pathlib.Path(
        "/juno/u/tylerlum/github_repos/nerf_grasping/experiments/2024-05-02_16-19-22/nerf_to_mesh/mug_330/coacd/decomposed.obj"
    ),
    obj_xyz: Tuple[float, float, float] = (0.65, 0.0, 0.0),
    obj_quat_wxyz: Tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0),
    collision_check_table: bool = True,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, float, MotionGenResult]:
    assert X_W_H.shape == (4, 4), f"X_W_H.shape: {X_W_H.shape}"
    trans = X_W_H[:3, 3]
    rot_matrix = X_W_H[:3, :3]
    quat_wxyz = transforms3d.quaternions.mat2quat(rot_matrix)

    target_pose = Pose(
        torch.from_numpy(trans).float().cuda(),
        quaternion=torch.from_numpy(quat_wxyz).float().cuda(),
    )

    tensor_args = TensorDeviceType()
    robot_file = "fr3_algr_zed2i.yml"
    robot_cfg = RobotConfig.from_dict(
        load_yaml(join_path(get_robot_configs_path(), robot_file))["robot_cfg"]
    )

    # Apply joint limits
    if q_algr_constraint is not None:
        assert q_algr_constraint.shape == (
            16,
        ), f"q_algr_constraint.shape: {q_algr_constraint.shape}"
        assert robot_cfg.kinematics.kinematics_config.joint_limits.position.shape == (2, 23)
        robot_cfg.kinematics.kinematics_config.joint_limits.position[0, 7:] = (
            torch.from_numpy(q_algr_constraint).float().cuda() - 0.01
        )
        robot_cfg.kinematics.kinematics_config.joint_limits.position[1, 7:] = (
            torch.from_numpy(q_algr_constraint).float().cuda() + 0.01
        )

    world_dict = {}
    if collision_check_table:
        world_dict.update(get_table_collision_dict())
    if collision_check_object and obj_filepath is not None:
        world_dict.update(
            get_object_collision_dict(file_path=obj_filepath, xyz=obj_xyz, quat_wxyz=obj_quat_wxyz)
        )
    world_cfg = WorldConfig.from_dict(world_dict)

    tensor_args = TensorDeviceType()
    motion_gen_config = MotionGenConfig.load_from_robot_config(
        robot_cfg,
        world_cfg,
        tensor_args,
        trajopt_tsteps=32,
        collision_checker_type=CollisionCheckerType.MESH,
        use_cuda_graph=True,
    )
    motion_gen = MotionGen(motion_gen_config)
    motion_gen.warmup()

    if q_fr3_start is None:
        q_fr3_start = DEFAULT_Q_FR3
    if q_algr_constraint is None:
        q_algr_constraint = DEFAULT_Q_ALGR
    start_q = np.concatenate([q_fr3_start, q_algr_constraint])
    start_state = JointState.from_position(torch.from_numpy(start_q).float().cuda().view(1, -1))

    t_start = time.time()
    result = motion_gen.plan_single(
        start_state=start_state,
        goal_pose=target_pose,
        plan_config=MotionGenPlanConfig(
            enable_graph=True,
            enable_opt=False,
            max_attempts=10,
            num_trajopt_seeds=10,
            num_graph_seeds=10,
        ),
    )
    print("Time taken: ", time.time() - t_start)
    print("Trajectory Generated: ", result.success)
    if result is None:
        raise RuntimeError("IK Failed")
    if not result.success:
        raise RuntimeError("Trajectory Optimization Failed")
    traj = result.get_interpolated_plan()

    n_timesteps = traj.position.shape[0]
    assert traj.position.shape == (n_timesteps, 23)
    assert traj.velocity.shape == (n_timesteps, 23)
    assert traj.acceleration.shape == (n_timesteps, 23)
    assert result.optimized_dt is not None
    return (
        traj.position.detach().cpu().numpy(),
        traj.velocity.detach().cpu().numpy(),
        traj.acceleration.detach().cpu().numpy(),
        result.optimized_dt,
        result,
    )


def main() -> None:
    X_W_H_feasible = np.array(
        [
            [0, 0, 1, 0.4],
            [0, 1, 0, 0.0],
            [-1, 0, 0, 0.15],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    X_W_H_collide_object = np.array(
        [
            [0, 0, 1, 0.65],
            [0, 1, 0, 0.0],
            [-1, 0, 0, 0.15],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    X_W_H_collide_table = np.array(
        [
            [0, 0, 1, 0.4],
            [0, 1, 0, 0.0],
            [-1, 0, 0, 0.10],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    q_algr_pre = np.array(
        [
            0.29094562,
            0.7371094,
            0.5108592,
            0.12263706,
            0.12012535,
            0.5845135,
            0.34382993,
            0.605035,
            -0.2684319,
            0.8784579,
            0.8497135,
            0.8972184,
            1.3328283,
            0.34778783,
            0.20921567,
            -0.00650969,
        ]
    )

    q, qd, qdd, dt, result = solve_trajopt(
        X_W_H=X_W_H_feasible,
        q_algr_constraint=q_algr_pre,
    )
    print(f"q.shape = {q.shape}, qd.shape = {qd.shape}, qdd.shape = {qdd.shape}, dt = {dt}")

    try:
        q, qd, qdd, dt = solve_trajopt(
            X_W_H=X_W_H_collide_object,
            q_algr_constraint=q_algr_pre,
        )
        raise ValueError("Collision with object should have failed")
    except RuntimeError as e:
        print(f"Collision with object failed as expected: {e}")


if __name__ == "__main__":
    main()
