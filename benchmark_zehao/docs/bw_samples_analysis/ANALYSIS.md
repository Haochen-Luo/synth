# 纯黑/纯白 FPV 帧成因分析 (eval_30B_333_v2, 2026-06-07)

数据: HK `eval_30B_333_v2`, 已完成 155 任务。逐帧扫描 FPV thumbnail
(`scan_blackwhite.py`: 均值<12=BLACK, >243=WHITE, 或 >85% 像素打到轨道)。

## 量级
- **54/155 任务 (35%) 含异常帧** —— 远高于 README 06-07 说的 "~7%"。
  (README 的 7% 应该是肉眼粗看的低估; 逐帧统计后异常面要大得多。)
- 含 BLACK 帧: 28 任务; 含 WHITE 帧: 30 任务 (有重叠)。
- 异常帧长度差异极大: 从单帧瞬态 (case022, 1帧) 到几乎整局 (case37-L4 116帧, case069-L4 56帧)。

## 关键方法: 用 bird 视图证伪"相机穿模"
FPV 黑/白本身无法区分成因。但 **bird (俯视) 视图永远正常**, 能看到 agent
相对几何体的真实位置。规则:
- 若 bird 显示 agent **紧贴墙/嵌进家具** → 真·相机穿模 (camera clipping)。
- 若 bird 显示 agent **站在开阔地、四周无遮挡** 却 FPV 全黑/白 → **不是穿模**,
  是场景照明问题 (fill light 过曝 / 场景过暗 / 近距离薄表面)。

## 三类成因 (实证)

### 类型 A — 真·相机穿模 (camera clipping) ✅ 用户观察正确
**证据: `case12_text_bedroom_04_minimal_study-L4`** (74帧黑, 33次全 static 碰撞)
- bird `0058` / `0059`: agent 紧贴房间**下边缘墙**, 正面朝墙, 没移动。
- FPV `0058`: 相机已**半穿进墙角** —— 左下半屏纯白(近裁剪面0.01m切进墙体,
  看到被 fill light 打爆的墙内表面), 右上还剩一条地板/墙接缝。
- FPV `0059`+: 完全陷入 → 全黑。
- 机制 = README 说的: 碰撞 sweep 只在 z=0.5/1.0 两高度做 (球半径0.40,
  球顶~1.4m), 相机在 z=1.58 —— **1.4~1.58m 存在盲区**。遇到齐胸高/悬空
  家具或不平的墙, sweep 放行但相机穿进去。

**`USER_FLAGGED__case18_dining_push_lift-L2`** (用户确认穿模, 71帧黑):
- bird 显示 agent 在房间中部, 正前方紧贴一个矮家具(ottoman/凳);
  FPV 从可见骤降到全黑。属同一机制 (贴前方近物, 相机切进)。

> A 类是真 benchmark 噪声中**最该担心**的: 盲区穿模时 agent 常**收不到**
> `blocked by an obstacle` 文本反馈 (sweep 在它的高度没命中), 模型纯粹被坑。
> 印证于 case061-L2: 101帧黑但仅 22 次 static 碰撞 —— 黑帧数 ≫ 碰撞数。

### 类型 B — 场景过暗 + fallback 灯不足 (非穿模)
**证据: `case069_official_solo_run-L4`** (用户原以为穿模, 实为此类)
- bird `0034` / `0113`: agent 站在**房间正中央开阔地板**, 四周不贴任何东西。
- FPV `0033`: 已近全黑, 只看得到 5 个亮球 = **fallback SphereLight (intensity 80000)**。
- FPV `0034`: 突然全黑 (run_dance 场景, 疑似 dancer 大网格瞬间挡住相机/遮挡 fill light)。
- 机制: 场景原生光照不足, 全靠 spawn 点附近 5 盏 fill light 硬撑; agent 一旦
  走离 spawn / 被动态人遮挡, FPV 就黑。**不是穿模** (bird 证明 agent 在空地)。

`case061_official_solo_run-L2` 同属偏暗类 (L形房间, 紫色调=只有 fill light)。

### 类型 C — fill light 过曝 (非穿模, 多发生在 spawn / 贴近薄面)
**证据: `case03_scene_gen_v5_test_input_case2-L2`** (开局 0000 即白)
- bird `0000`: spawn 在开阔客厅一角, 但 L2 `spawn_facing=back` 让相机正对墙角。
- FPV `0000`: 正对墙角且很近, 被 fill light 打到接近过曝 (看得到踢脚线)。
- 机制 = README §"PathTracing Fallback Lighting Overexposure": task spec 缺
  `scene_objects` bounds → fill light fallback **每次必触发**; 灯落墙里 → 局部过曝。

**`case37_mask_dining_04_gallery_table-L4`** (116帧黑白混, 39次全static):
- bird `0021`/`0027`: agent 在**明亮 dining gallery 正中央, 没动**。
- FPV `0023`=纯白, `0027`=纯黑, 同一位置先白后黑。
- 这是 A/C 混合: agent 怼着一个看不见的薄障碍来回蹭 (39次static全在原地),
  相机贴面 → 朝亮面=白, 朝背光面=黑。bird 证明它没真嵌进大家具, 是贴薄面。

## 对 README 06-07 那段 AI 分析的最终评价
- **机制方向对** (相机高于碰撞 sweep → 悬空/齐胸物穿模): ✅ case12-L4 实锤。
- **量级低估**: 说 ~7%, 实测含异常帧任务达 35%。
- **过曝归因不全**: 它说"相机穿进 vanity lamp", 但主因其实是 **fill light fallback
  每帧必触发** (case03/case37), 不是 agent 撞进灯。
- **黑屏归因不全**: 不全是 clipping —— 还有**场景过暗+fill光不足** (case069) 和
  (README 别处记的) MirroredBall 不支持导致镜/窗黑面。
- **"不影响评测"过于乐观**: A类盲区穿模时 agent 常收不到 blocked 反馈
  (case061: 101黑帧 vs 22碰撞), 这就是真 benchmark 噪声, 非纯"模型空间推理差"。

## 建议修复 (按性价比)
1. **补一条相机高度的碰撞 sweep**: 在 z=1.58 (EYE_H) 增加一档 sweep, 或把现有
   sweep 半径/高度覆盖到相机高度, 消除 1.4~1.58m 盲区 → 直接堵住 A 类穿模
   + 让 agent 至少能收到 blocked 反馈。 (bench_runner.py L1621 / L922 / L1177)
2. **fill light fallback 改良**: 不再每帧无脑放 5 盏 80000 的 SphereLight;
   用 Infinigen `_floor` mesh 算真实房间 bounds 放天花板 RectLight, 避免落墙里
   → 消 C 类过曝。 (README §PathTracing Fallback 已记此 TODO)
3. **暗场景**: B 类需要给原生光照不足的场景兜底环境光 (低强度 DomeLight/ambient),
   而非只靠 spawn 附近 fill light。

## 文件
- `scan_blackwhite.py` / `collect_frames.py` (HK 端扫描+取帧脚本, 已存 /tmp 与本目录)
- `MANIFEST.txt` / `MANIFEST.json` (8个代表任务的异常段+metrics)
- `<tag>__<case>/fpv/`, `/bird/`: 各任务异常段前后帧 (FPV与bird同名配对, 文件名带帧号+B/W标注)
- `USER_FLAGGED__case18.../`: 用户确认的穿模案例 (含原 mp4 preview)
