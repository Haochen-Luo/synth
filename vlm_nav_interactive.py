# %% [markdown]
# # Cell 1: 启动物理引擎 (仅需运行一次)
# 注意：这个 Cell 需要等待大约 1 分钟。只要它运行成功，后续所有的 Cell 都是秒开的！

# %%
import os
import math
import time
import glob
import urllib.request
import json
from isaacsim import SimulationApp

# 启动引擎（全局仅此一次）
simulation_app = SimulationApp({"headless": True})

import omni.usd
import omni.replicator.core as rep
from pxr import UsdGeom, Gf

# 配置 VLLM API
VLLM_URL = "http://localhost:8300/v1/chat/completions"
MODEL_NAME = "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8"

print("引擎启动完毕！")

# %% [markdown]
# # Cell 2: 加载场景与初始化物理模型
# 如果你需要更换测试的房间（改变 USD 路径），可以重新运行这个 Cell。

# %%
scene_dir = "/home/qi/hc/Puppeteer/zehao_new_folder/phy_env/case11_multi_surface_turn_right_full_physics_scene"
usd_path = os.path.join(scene_dir, "compiled_stages", "case11_multi_surface_turn_right_full_physics.compiled.usda")

omni.usd.get_context().open_stage(usd_path)
stage = omni.usd.get_context().get_stage()

# 强制开启最高画质的光线追踪
import omni.kit.commands
omni.kit.commands.execute("ChangeSetting", path="/rtx/rendermode", value="PathTracing")
omni.kit.commands.execute("ChangeSetting", path="/rtx/pathtracing/spp", value=16)

# 添加神明灯光，确保天花板掀开后依然明亮
light_positions = [(5,6,2.3), (10,6,2.3), (15,6,2.3), (5,2,2.3), (10,10,2.3)]
for lp in light_positions:
    rep.create.light(light_type="sphere", position=lp, intensity=50000.0)

# 加载动态跑步者的数据
dynamic_json = os.path.join(scene_dir, "compiled_stages", "case11_multi_surface_turn_right_full_physics_dynamic.json")
humans_spec = []
if os.path.exists(dynamic_json):
    with open(dynamic_json, "r") as f:
        dynamic_data = json.load(f)
        humans_spec = dynamic_data.get("humans", [])

# 动作插值函数（用于障碍物跑步者）
def lerp(a, b, t): return a + (b - a) * t
def wrap_angle_deg(a): return (a + 180.0) % 360.0 - 180.0
def lerp_angle_deg(a, b, t):
    diff = wrap_angle_deg(b - a)
    return wrap_angle_deg(a + diff * t)
def sample_human_motion(spec, t, fps=30.0):
    start_tc = spec["animation"]["start_time_code"]
    end_tc = spec["animation"]["end_time_code"]
    frames = end_tc - start_tc
    loop_dur = frames / fps
    local_t = t % loop_dur
    rev = False
    if spec["animation"].get("trajectory_loop_mode") == "ping_pong_180_turn":
        local_t = t % (2 * loop_dur)
        if local_t > loop_dur:
            local_t = 2 * loop_dur - local_t
            rev = True
    frame_idx = local_t * fps
    fi = int(math.floor(frame_idx))
    alpha = frame_idx - fi
    fi = min(fi, int(frames)-1)
    fj = min(fi + 1, int(frames)-1)
    
    sa = spec["keyframes"][fi]
    sb = spec["keyframes"][fj]
    ta = list(sa.get("root_pos", [0,0,0]))
    tb = list(sb.get("root_pos", ta))
    ra = list(sa.get("rotation_deg_xyz", [0,0,0]))[:3]
    rb = list(sb.get("rotation_deg_xyz", ra))[:3]
    pos = [lerp(ta[i],tb[i],alpha) for i in range(3)]
    rot = [lerp(ra[0],rb[0],alpha), lerp(ra[1],rb[1],alpha), lerp_angle_deg(ra[2],rb[2],alpha)]
    if rev: rot[2] = wrap_angle_deg(rot[2]+180.0)
    return pos, rot

print("场景和灯光加载完毕！")

# %% [markdown]
# # Cell 3: 部署相机与角色
# 这个 Cell 会重置 Agent 和障碍物的位置，并配置好完美的“上帝视角”。
# 每次你想重新跑一次 Benchmark 时，从这个 Cell 开始运行即可！

# %%
STEP_DISTANCE = 0.25
TURN_ANGLE = 30.0
MAX_STEPS = 250
SUCCESS_RADIUS = 0.8     # meters to target for success
AGENT_HEIGHT = 0.0       # Fix: match dancer's physical height
AGENT_EYE_HEIGHT = 1.58  # z for camera (eye level)模拟人类第一人称视线高度
RUNNER_TIME_PER_STEP = 0.5

TARGET = [4.38, 6.44] # 沙发中心
AGENT_START_X = 12.0
AGENT_START_Y = 4.0
AGENT_START_YAW = 160.0

out_dir_fpv = "/home/qi/hc/Puppeteer/zehao_task/vlm_nav_frames_fpv"
out_dir_bird = "/home/qi/hc/Puppeteer/zehao_task/vlm_nav_frames_bird"
os.makedirs(out_dir_fpv, exist_ok=True)
os.makedirs(out_dir_bird, exist_ok=True)

# 1. 部署第一人称相机
cam_fpv = rep.create.camera(
    position=(AGENT_START_X, AGENT_START_Y, AGENT_EYE_HEIGHT),
    rotation=(0,0,0),
    name="NavCamera"
)
rp_fpv = rep.create.render_product(cam_fpv, (1920, 1080))
writer_fpv = rep.WriterRegistry.get("BasicWriter")
writer_fpv.initialize(output_dir=out_dir_fpv, rgb=True)
writer_fpv.attach([rp_fpv])

# 2. 部署上帝视角
cam_bird = rep.create.camera(
    position=(13.0, 7.0, 2.7),  
    look_at=(5.0, 5.0, 0.5),
    name="BirdEyeCamera"
)
rp_bird = rep.create.render_product(cam_bird, (1920, 1080))
writer_bird = rep.WriterRegistry.get("BasicWriter")
writer_bird.initialize(output_dir=out_dir_bird, rgb=True)
writer_bird.attach([rp_bird])

# 3. 部署障碍物与Agent
human_usd = "/home/qi/hc/Puppeteer/zehao_new_folder/phy_env/case11_multi_surface_turn_right_full_physics_scene/assets/humans/obj_1_run_anim_1.usdc"
agent_prim = stage.DefinePrim("/World/Humans/agent_runner")
agent_prim.GetReferences().AddReference(human_usd)

runner1_spec = next((h for h in humans_spec if "run" in h.get("name", "")), None)
runner1_xform = None
if runner1_spec:
    name = runner1_spec["name"].replace(" ", "_")
    p = stage.DefinePrim(f"/World/Humans/{name}")
    p.GetReferences().AddReference(os.path.join(scene_dir, runner1_spec["runner_usd_path"]))
    xf = UsdGeom.Xformable(p)
    t = xf.AddTranslateOp()
    o = xf.AddOrientOp()
    s = xf.AddScaleOp()
    s.Set(Gf.Vec3d(0.01, 0.01, 0.01))
    runner1_xform = {"spec": runner1_spec, "trans_op": t, "orient_op": o}

# === Setup Agent (Runner 2) ===
agent_xf = UsdGeom.Xformable(agent_prim)
try: agent_xf.ClearXformOpOrder()
except: pass
agent_trans = agent_xf.AddTranslateOp()
agent_orient = agent_xf.AddOrientOp()
agent_scale = agent_xf.AddScaleOp()
agent_scale.Set(Gf.Vec3d(0.01, 0.01, 0.01)) # FORCE 0.01 scale

print("相机和模型摆放完毕！可以开始推演了。")

# %% [markdown]
# # Cell 4: 运行 VLM 闭环导航
# 这个 Cell 包含了所有物理碰撞检测和 VLM API 调用的核心逻辑。
# 你可以直接反复运行这个 Cell 来测试不同的提示词或大模型！

# %%
SYSTEM_PROMPT = """You are a navigation robot inside a living room. Your goal is to reach the SOFA.

You see the room from your first-person camera. There may be a person running across the room - you must avoid colliding with them.

You can ONLY output ONE of these actions:
- MOVE_FORWARD
- TURN_LEFT
- TURN_RIGHT
- STOP

Do NOT output any other text, reasoning, or markdown. Output exactly the action string."""

def query_vlm(image_path, collision_alert=False):
    import base64
    with open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode('utf-8')
    
    prompt = SYSTEM_PROMPT
    if collision_alert:
        prompt += "\nWARNING: Your previous path was blocked! You bumped into an obstacle or wall. You must TURN_LEFT or TURN_RIGHT to find a clear path before moving forward again!"
        
    messages = [
        {"role": "system", "content": [{"type": "text", "text": prompt}]},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
            {"type": "text", "text": "What is your next action?"}
        ]}
    ]
    data = json.dumps({"model": MODEL_NAME, "messages": messages, "max_tokens": 10, "temperature": 0.0}).encode('utf-8')
    req = urllib.request.Request(VLLM_URL, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"VLM Error: {e}")
        return "MOVE_FORWARD"

# Find camera prim for per-step repositioning
nav_cam_prim = next((p for p in stage.Traverse() if "NavCamera" in str(p.GetPath())), None)
timeline = omni.timeline.get_timeline_interface()
timeline.play()

agent_x, agent_y, agent_yaw = AGENT_START_X, AGENT_START_Y, AGENT_START_YAW
sim_time = 0.0
nav_history = []
collision_occurred = False

print("=== 开始 VLM 导航测试 ===")

for step in range(MAX_STEPS):
    agent_trans.Set(Gf.Vec3d(agent_x, agent_y, AGENT_HEIGHT))
    yaw_rad = math.radians(agent_yaw)
    agent_orient.Set(Gf.Quatf(math.cos(yaw_rad/2), 0, 0, math.sin(yaw_rad/2)))
    
    # [FIX]: Native USD LookAt algorithm
    if nav_cam_prim and nav_cam_prim.IsValid():
        cam_xf = UsdGeom.Xformable(nav_cam_prim)
        try: cam_xf.ClearXformOpOrder()
        except: pass
        cam_trans = cam_xf.AddTranslateOp()
        cam_orient = cam_xf.AddOrientOp()
        
        cam_trans.Set(Gf.Vec3d(agent_x, agent_y, AGENT_EYE_HEIGHT))
        
        # Explicit Euler construction (Yaw = agent_yaw)
        rot_z = Gf.Rotation(Gf.Vec3d(0, 0, 1), float(agent_yaw) - 90)
        quat = rot_z.GetQuat()
        cam_orient.Set(Gf.Quatf(quat.GetReal(), *quat.GetImaginary()))
        
    if runner1_xform:
        pos, rot_deg = sample_human_motion(runner1_xform["spec"], sim_time, 30.0)
        runner1_xform["trans_op"].Set(Gf.Vec3d(pos[0], pos[1], pos[2]))
        r_yaw = math.radians(rot_deg[2] + runner1_xform["spec"].get("visual_rotation_offset_deg_xyz", [0,0,0])[2])
        runner1_xform["orient_op"].Set(Gf.Quatf(math.cos(r_yaw/2), 0, 0, math.sin(r_yaw/2)))
        
    rep.orchestrator.step(rt_subframes=16)
    
    # 获取图片并调用大模型
    import glob
    pngs = sorted(glob.glob(os.path.join(out_dir_fpv, "rgb_*.png")))
    if not pngs: break
    frame_path = pngs[-1]
    
    dist_to_target = math.sqrt((agent_x - TARGET[0])**2 + (agent_y - TARGET[1])**2)
    print(f"Step {step}: 坐标({agent_x:.2f},{agent_y:.2f}) 朝向={agent_yaw:.0f}° 距离沙发={dist_to_target:.2f}m")
    
    action = query_vlm(frame_path, collision_alert=collision_occurred)
    collision_occurred = False
    print(f"VLM 决定: {action}")
    
    nav_history.append({
        "step": step,
        "x": round(agent_x, 3),
        "y": round(agent_y, 3),
        "yaw": round(agent_yaw, 1),
        "dist_to_target": round(dist_to_target, 3),
        "action": action,
    })
    
    if action == "STOP":
        print("VLM 认为已到达目标！")
        break
        
    if action == "MOVE_FORWARD":
        import omni.physx
        query = omni.physx.get_physx_scene_query_interface()
        dir_x = math.cos(math.radians(agent_yaw))
        dir_y = math.sin(math.radians(agent_yaw))
        
        # Physics Sweep Sphere (radius=0.2m)
        origin = carb.Float3(agent_x, agent_y, 0.5)
        direction = carb.Float3(dir_x, dir_y, 0.0)
        hit = query.sweep_sphere_closest(0.2, origin, direction, STEP_DISTANCE)
        
        if not hit["hit"]:
            agent_x += STEP_DISTANCE * dir_x
            agent_y += STEP_DISTANCE * dir_y
        else:
            print(">>> 警告: 前方有障碍物/墙壁！已被 PhysX 拦截！")
            collision_occurred = True
    elif action == "TURN_LEFT":
        agent_yaw += TURN_ANGLE
    elif action == "TURN_RIGHT":
        agent_yaw -= TURN_ANGLE
        
    sim_time += RUNNER_TIME_PER_STEP

print("=== 测试结束 ===")
with open("/home/qi/hc/Puppeteer/zehao_task/vlm_nav_history.json", "w") as f:
    json.dump(nav_history, f, indent=4)
print("导航日志已保存到 vlm_nav_history.json")

# %% [markdown]
# # Cell 5: 生成最终视频 (MP4/GIF)
# 导航结束后，运行此 Cell 把所有抓拍的帧合成为视频文件。

# %%
import subprocess
import glob, os

print("开始使用 FFmpeg (pp env) 合成高清和轻量预览视频...")
for prefix, out_dir in [("fpv", out_dir_fpv), ("birdseye", out_dir_bird)]:
    frames = sorted(glob.glob(os.path.join(out_dir, "rgb_*.png")))
    if not frames: continue
    
    mp4_hd = f"/home/qi/hc/Puppeteer/zehao_task/demo_{prefix}_hd.mp4"
    mp4_lite = f"/home/qi/hc/Puppeteer/zehao_task/demo_{prefix}_lite.mp4"
    gif_hd = f"/home/qi/hc/Puppeteer/zehao_task/demo_{prefix}_hd.gif"
    gif_lite = f"/home/qi/hc/Puppeteer/zehao_task/demo_{prefix}_lite.gif"
    
    print(f"正在生成 {prefix} 视角的媒体文件...")
    cmd = f"""source /home/qi/miniconda3/etc/profile.d/conda.sh && conda activate pp && \\
ffmpeg -y -hide_banner -loglevel error -framerate 10 -pattern_type glob -i '{out_dir}/rgb_*.png' -c:v libx264 -pix_fmt yuv420p -crf 18 {mp4_hd} && \\
ffmpeg -y -hide_banner -loglevel error -framerate 10 -pattern_type glob -i '{out_dir}/rgb_*.png' -vf 'scale=640:-2' -c:v libx264 -pix_fmt yuv420p -crf 28 {mp4_lite} && \\
ffmpeg -y -hide_banner -loglevel error -framerate 10 -pattern_type glob -i '{out_dir}/rgb_*.png' -vf 'fps=10,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse' -loop 0 {gif_hd} && \\
ffmpeg -y -hide_banner -loglevel error -framerate 10 -pattern_type glob -i '{out_dir}/rgb_*.png' -vf 'fps=10,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse' -loop 0 {gif_lite}"""
    
    subprocess.run(cmd, shell=True, executable='/bin/bash')
    print(f"[{prefix}] 生成成功！")

from IPython.display import Video, display
print("生成完毕！双击左侧目录下的 MP4/GIF 文件即可查看。")
try:
    # 默认预览 Lite 轻量级版本保证浏览器不卡顿
    display(Video("/home/qi/hc/Puppeteer/zehao_task/demo_fpv_lite.mp4", embed=True, width=640))
except:
    pass
