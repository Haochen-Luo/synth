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

> ⚠️ 重要更正 (2026-06-08, 经 Isaac 实测 + 真实 repro): 下面早期把 case12 归为
> "相机高度盲区穿模(EYE_H)" 的判断是 **错的**。真正根因经实证为
> **掠射角(grazing-angle) + 零厚度薄壳墙(approximation=none) 导致 sweep 漏检**。
> 详见文末 "## 根因实证 (case12-L4)"。早期 A 类描述保留作为推理过程记录。

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

---

## 根因实证 (case12-L4) — 2026-06-08

经过多轮纠错(穿透→视觉错位→高度盲区,均被推翻)和一次真实环境 repro,
最终用实测数据确定了根因。**结论:掠射角 + 零厚度薄壳墙导致 sweep 漏检。**

### 调查方法
1. Isaac 几何探测: 墙 `bedroom_0_0_wall` 真实世界 bbox → 内表面在 **y=0.14**,
   approximation=`none`(零厚度三角网格薄壳)。
2. 原始 case12 nav_history: agent 圆心最终在 **(5.30, 0.021)**,y=0.021 < 0.14
   → **圆心物理性地进入了墙体 0.12m**(不是视觉错位)。
3. 真实 repro(`repro_badcase.sh` + `SWEEP_DEBUG=1`, 隔离 batch `_repro_sweep_debug`):
   在真实 bench_runner 里打印每次 MOVE_FORWARD 的 sweep 返回值。

### 决定性证据 (repro 真实 sweep 日志)
agent **正面逼近墙** 时,sweep 完全正常、正确 block:
```
step25 pos=(3.048,0.597) dir=(+0.79,-0.61) z=0.5 hit=True dist=0.0941 path=bedroom_0_0_wall
step27 pos=(3.048,0.597) dir=(+0.92,-0.38) z=0.5 hit=True dist=0.1497 path=bedroom_0_0_wall
```
→ agent 被挡在 y=0.597,**从未进墙**(min y=0.60)。**PhysX 工作正常,墙能测距。**

### 原始那次为何穿墙 — 掠射角
原始 nav_history step 56→59 的移动方向:
```
step58 pos=(5.763,0.212) yaw=-157.5 dir=(-0.92,-0.38) moved=True blocked=False
step59 pos=(5.532,0.117) yaw=-157.5 dir=(-0.92,-0.38) moved=True blocked=False  <- 越过 y=0.14
```
墙是 y-法线的墙,但移动方向 dir=(-0.92, **-0.38**) —— **主分量是 -x(沿墙),
朝墙的 y 分量只有 0.38(掠射角)**。`sweep_sphere_closest` 对零厚度薄壳在
掠射角下漏检 → 返回 no-hit → 放行 → 圆心从 y=0.212 滑过墙面到 y=0.117 → 再到
y=0.021(深入墙内)。进墙后从内侧 sweep 返回 dist=0 → 卡住出不来。

### 解答"物理失效却记录 collision"的矛盾
物理 **没有** 失效。collision 依赖 sweep,sweep 真实工作:
- 墙外正面 → 正常测距 block(dist=0.09/0.15)。
- 墙外掠射 → 漏检放行(穿墙)。
- 墙内 → dist=0 退化命中 → 记录 collision + 卡住。
原始 case12 的 33 次命中全是 dist=0.000,正因为它们都发生在 agent **已在墙内**
之后;agent 在墙外掠射穿入的那几步反而没有命中(漏检)。

### 已纠正的错误判断
| 早期判断 | 状态 | 纠正依据 |
|---|---|---|
| 薄壳穿透(tunneling) | 部分对 | 是掠射角下的穿透,非任意穿透 |
| 纯视觉错位(碰撞球比mesh短) | 错 | 圆心 y=0.021 真在墙内 |
| 高度盲区(EYE_H sweep 修复) | 错 | 正面 z=0.5 sweep 正常工作;问题在水平掠射 |
| 物理完全不生效 | 错 | repro 实测墙能正常测距 block |
| **掠射角 + 薄壳漏检** | **确认** | repro 正面正常 + 原始掠射穿入,双向实证 |

### 修复方向 (对症)
EYE_H sweep(已提交 commit `c597755`)**治不了这个**,应撤回或仅作附加保险。
对症修复应针对掠射角漏检,可选:
1. **floor-polygon 夹紧**: 每步走完后用 floor 多边形(实测 x/y∈[0.14,7.86])做
   point-in-polygon, 圆心(留 0.4 半径余量)出界则回退+报 blocked。纯几何、不依赖
   PhysX 对薄壳的掠射行为, 最稳。
2. **多向 sweep**: 除移动方向外, 额外向墙法线方向补一次 sweep, 但需先知道墙法线。
3. **collider 改 convex/box**: probe 阶段把 wall/exterior 的 approximation 从 none
   改 convexHull, 给墙体积, 消除薄壳掠射漏检。改动面大。

### 诊断工具 (已加, env 门控, 默认关, 对正式 eval 零影响)
- `bench_runner.py`: `SWEEP_DEBUG=1` → 打印每次 MOVE_FORWARD 的逐高度 sweep 结果。
- `repro_badcase.sh`: 隔离 batch 复现单个 bad-case task。

---

## 真·根因 (case12-L4) — 2026-06-08 第二轮, 铁证

⚠️ 上一节的"掠射角"假设也被推翻了 (angle 实验显示 5.76,0.21 处所有入射角
0~90° 都 hit=True, 无角度漏检)。经逐步复现原始 nav_history 的精确 sweep, 确定
真根因, 复现结果与原始 blocked 标记**逐步精确一致**(55-59 PASS, 60/62 BLOCK)。

### 根因 = WALKABLE 子串误匹配 + dist=0 命中遮蔽 (两 bug 叠加)

**复现的真实 sweep 返回 (env 经 orchestrator.step 修正后可信):**
```
step55-59 (走进墙的几步): 每个高度 hit=True dist=0.0000
          命中物 = Obj_382501_FloorLampFactory  标记 (walkable-skip)  => PASS 放行
step60/62 (已在墙内): 命中变成 skirtingboard_support / bedroom_0_0_wall  => BLOCK
```

**Bug 1 — WALKABLE 子串误匹配 (致命):**
```python
WALKABLE = ("floor","ground","rug","blanket","towel","mat")
if any(w in hit_path for w in WALKABLE): continue   # 子串 in 匹配
```
`"floorlampfactory"` 包含子串 `"floor"` → 落地灯被误判为可走地面 → 跳过/放行。
同类误伤 (已验证): `BathMatFactory`/`DoormatFactory` 含 `"mat"`; `RugFactory` 含 `"rug"`(这个恰好对)。

**Bug 2 — dist=0 命中遮蔽 (放大器):**
`sweep_sphere_closest` 只返回**最近一个**命中。agent 走到 (5.76,0.21) 时球已
嵌入落地灯 collider → PhysX 返回 dist=0.0000 的落地灯作为"最近命中" → **前方
0.1m 的墙被这个 dist=0 命中遮蔽, 根本没进入返回值**。代码只看到落地灯一条信息。

**叠加后果:** sweep 只报落地灯(墙被遮蔽) + 落地灯又被子串 bug 误判可走 →
代码以为"前方完全通畅" → 放行 → agent 穿过落地灯连同被遮蔽的墙一起走进墙内
(圆心到 y=0.021)。进墙后转向, 最近命中变成 wall/skirting(非 walkable)才 BLOCK,
但人已卡在墙里 → 持续黑屏 + 全 dist=0 的 collision 记录。

### 为什么这次确信 (区别于之前的自洽假说)
1. 逐步复现原始 nav_history 精确坐标+方向, sweep 返回的 PASS/BLOCK 序列与原始
   nav_history 的 blocked 标记**逐步完全吻合** → 环境可信、机制重现。
2. 命中物 = FloorLampFactory, 用代码独立验证 `"floor" in "floorlampfactory"`=True。
3. 是可定位到具体代码行 + 可复现的 bug, 非推测。

### 离线环境修正 (之前所有离线 MISS 失效的原因)
离线脚本必须 `import omni.replicator.core as rep` 并调用 `rep.orchestrator.step()`
若干次, collider 才注册到 PhysX scene query。纯 `sim_app.update()` 不激活物理 →
之前所有离线探测全 MISS (不可信)。实测: pre-orchestrator hit=False →
post-orchestrator(x4) hit=True d=0.091。

### 全部错误假设清算
| 假设 | 状态 |
|---|---|
| 薄壳穿透 / 视觉错位 / 高度盲区(EYE_H) / 物理全失效 / 掠射角漏检 | 全部错 |
| **WALKABLE 子串误匹配 + dist=0 遮蔽** | **铁证确认** |

### 修复方向
- **最小够用**: WALKABLE 子串匹配 → 精确/token 匹配 (mesh 名末段 == floor/ground/
  rug/mat, 或带词界), 排除 FloorLamp/BathMat/Doormat 误伤。改这一处即可阻止穿墙
  (落地灯被正确判障 → block, 即使墙仍被遮蔽也无妨)。
- **更彻底**: dist=0 退化命中时改用 overlap 查询拿全部重叠物逐个判断, 解决遮蔽。
- **EYE_H commit `c597755` 不对症, 应撤回。**
