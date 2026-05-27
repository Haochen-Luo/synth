import os
import shutil
import re

base_dir = "/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/results/overnight_pushpause_v1"
dest_dir = "/home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/results_overnight"

os.makedirs(dest_dir, exist_ok=True)
copied_count = 0

for level in ["L1", "L2", "L3", "L4"]:
    level_dir = os.path.join(base_dir, level)
    if not os.path.exists(level_dir):
        continue
    for case_folder in sorted(os.listdir(level_dir)):
        case_path = os.path.join(level_dir, case_folder)
        if not os.path.isdir(case_path):
            continue
        
        # Extract case name without timestamp (e.g., case01-L1_20260523_175736 -> case01-L1)
        case_name = re.sub(r'_\d{8}_\d{6}$', '', case_folder)
        
        src_file = os.path.join(case_path, "vlm_nav_frames_bird", "rgb_0000_thumb.jpg")
        if os.path.exists(src_file):
            dest_file = os.path.join(dest_dir, f"{level}_{case_name}_bird_rgb__0000_thumb.jpg")
            shutil.copy2(src_file, dest_file)
            print(f"Copied: {case_folder}/.../rgb_0000_thumb.jpg -> {os.path.basename(dest_file)}")
            copied_count += 1

print(f"Finished copying {copied_count} files to {dest_dir}.")
