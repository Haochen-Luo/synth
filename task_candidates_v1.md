# 室内多步任务设计：超越纯导航的 VLM 任务拆解

## 背景 & 目标

当前系统已实现 **VLM → 离散动作 → Isaac Sim** 的闭环导航。Agent 能够到达指定目标（sofa / bookshelf），但所有任务本质上仍是 **单目标导航**（Go-To-X）。

**核心目标**：设计一系列需要 VLM **自主拆解子目标（subtask decomposition）** 的室内任务。VLM 不再只是"看到目标→走过去"，而是需要理解一个高层指令，推理出需要先做什么、再做什么，并在执行过程中根据视觉反馈动态调整。

> [!IMPORTANT]
> 不涉及细粒度物理交互（如手抓物体、流体模拟）。所有"交互"通过 **proximity trigger**（靠近触发）实现：Agent 走到某物体旁 → 系统自动执行状态变化（物体出现/消失/移动/动画切换）。VLM 的挑战在于**理解任务、规划路线、按正确顺序访问交互点**。

---

## 系统能力边界

### ✅ 当前已有
- 离散导航动作（MOVE_FORWARD / TURN_LEFT / TURN_RIGHT / STOP）
- PhysX 碰撞检测（sphere sweep）
- 动画人物（run / dance 带骨骼动画的 USDC）
- 鸟瞰 + 第一人称渲染
- VLM 闭环推理

### 🔧 需要新增（实现成本低）
- **Proximity Trigger 系统**：Agent 进入某半径 → 触发 USD 场景变化
- **扩展动作空间**：增加 `INTERACT` 动作（当 Agent 在交互点旁时生效）
- **多阶段任务状态机**：跟踪任务进度（哪些子目标已完成）
- **新 USDC 资产**：可以按需添加任意物体（家电、工具、容器等）

---

## 六大任务类型

### Tier 1：Sequential Multi-Stop（顺序多站点）

> **难度**：⭐⭐ | **拆解要求**：线性多步

#### 📋 任务："Emergency Kit Assembly / 急救包组装"
**指令**："Collect the first-aid supplies: go to the **medicine cabinet** (白色立柜), then pick up the **bandage roll** (桌上的绷带), then bring them to the **couch** where the injured person is sitting."

**VLM 需要拆解**：
1. 找到 medicine cabinet → 导航过去 → INTERACT（触发：柜门打开动画，药品出现在 Agent "手中"，即 UI overlay 显示已拾取）
2. 找到 bandage roll → 导航过去 → INTERACT（触发：绷带消失，已拾取）
3. 找到 couch + injured person → 导航过去 → INTERACT（触发：物品出现在伤者旁边，任务完成）

**需要的 USDC 资产**：
- 带门的立柜（可用现有 bookshelf 改造）
- 桌面小物品（绷带卷 → 简单圆柱体 USDC）
- 坐姿人物动画（sitting idle）

**为什么好**：
- 顺序依赖（必须先拿药再拿绷带，或者任意顺序拿完再送）
- 多目标搜索（3 个不同位置的物体）
- 视觉识别（VLM 需要区分柜子 vs 书架 vs TV柜）

---

### Tier 2：Conditional Branching（条件分支）

> **难度**：⭐⭐⭐ | **拆解要求**：观察 → 判断 → 选择不同路径

#### 📋 任务："Light Check / 检查照明"
**指令**："Check if the **desk lamp** (台灯) is on or off. If it's OFF, go to the **power switch** (墙壁开关) and turn it on. If it's already ON, go directly to the **reading desk** and report ready."

**VLM 需要拆解**：
1. 导航到台灯位置 → 观察灯是亮的还是暗的
2. **IF 暗**：导航到墙壁开关 → INTERACT（触发：台灯变亮，周围光照增强）→ 再去 reading desk
3. **IF 亮**：直接去 reading desk → STOP

**实现方式**：
- 场景随机初始化：50% 概率台灯区域有 SpotLight（亮），50% 没有（暗）
- VLM 必须从画面中判断灯的状态（这是纯视觉推理！）

**需要的 USDC 资产**：
- 台灯模型（放在 side table 上）
- 墙壁开关面板（简单方块贴图）
- 写字桌（可用 coffee table 代替）

**为什么好**：
- 真正的 **视觉条件判断**：VLM 需要"看"灯亮不亮
- 分支路径：不同观察结果 → 不同执行计划
- 模型不能死记硬背路线，必须 **在线推理**

---

### Tier 3：Social-Aware Navigation（社交感知导航）

> **难度**：⭐⭐⭐ | **拆解要求**：理解人类活动 → 调整行为

#### 📋 任务："Quiet Delivery / 安静配送"
**指令**："Deliver the **package** (包裹) from the **entrance table** (门口桌子) to the **bedroom shelf** (卧室架子). BUT: a person is **sleeping** on the couch — you must NOT get within 2m of the sleeper. Find an alternate route."

**VLM 需要拆解**：
1. 去入口桌子 → INTERACT（拾取包裹）
2. 识别沙发上的睡觉人物（lying down 动画）→ 理解"不能靠近"的约束
3. 规划绕行路线（从房间另一侧绕过沙发）
4. 到达卧室架子 → INTERACT（放下包裹）

**实现方式**：
- 沙发上放一个 lying-down 动画的人物（新动画 USDC）
- 系统端检测：如果 Agent 进入 sleeper 2m 范围 → prompt 注入 "⚠ You are too close to the sleeping person! Move away quietly."
- 评分：完成任务 + 全程保持 >2m 距离 = 满分

**需要的 USDC 资产**：
- 躺姿人物动画（sleeping / lying idle）
- 包裹模型（简单箱体）
- 入口桌 / 卧室架子（可复用现有家具）

**为什么好**：
- **空间约束推理**：VLM 不能走最短路径，需要理解"禁区"
- 结合人类活动理解（sleeping → 安静 → 避开）
- 路径规划复杂度上升

---

### Tier 4：Multi-Agent Coordination（多人场景理解）

> **难度**：⭐⭐⭐⭐ | **拆解要求**：理解多个人的状态 → 按优先级处理

#### 📋 任务："Room Service / 客房服务"
**指令**："There are 3 guests in the room. Deliver a **drink** to each guest. The guest who is **standing and waving** needs service first. The guest who is **sitting reading** can wait. The guest who is **exercising** should be served last (don't interrupt)."

**VLM 需要拆解**：
1. 去饮品桌 → INTERACT × 3（拿三杯饮品）
2. 环顾房间 → 识别三个人分别在做什么
   - 人物 A：standing + waving 动画 → **第一个**服务
   - 人物 B：sitting + reading 动画 → **第二个**
   - 人物 C：exercising 动画 → **最后一个**
3. 按优先级依次导航到每个人 → INTERACT（送饮品）

**实现方式**：
- 三个不同动画的人物 USDC（wave / sit-read / exercise）
- 随机放置在房间不同位置
- VLM 需要从画面识别动作类型

**需要的 USDC 资产**：
- 3种不同动画的人物（waving, sitting, exercising）
- 饮品台 / 托盘（简单桌面物体）

**为什么好**：
- **人类动作识别** + **优先级排序**：纯视觉推理
- 多目标调度问题
- 测试 VLM 的 "Theory of Mind" 能力

---

### Tier 5：State-Change Chain（状态链式反应）

> **难度**：⭐⭐⭐⭐ | **拆解要求**：理解因果链 → 按正确顺序操作

#### 📋 任务："Set Up Movie Night / 布置电影之夜"
**指令**："Prepare the living room for movie night: 1) Close the **curtains** (go to the window and interact), 2) Turn ON the **projector** (go to the projector on the shelf and interact — but it only works after curtains are closed!), 3) Arrange the **throw pillows** on the couch (go to the pillow basket near the bookshelf, then to the couch)."

**VLM 需要拆解**：
1. 找到窗帘 → INTERACT（触发：窗帘关闭，房间变暗 — 实际实现：移除 DomeLight / 降低 SphereLights）
2. 找到投影仪 → INTERACT  
   - **IF 窗帘已关闭**：投影仪开启（投影画面出现在墙壁上 — 实现：在墙面添加发光平面）
   - **IF 窗帘未关闭**：提示 "The projector says: TOO BRIGHT — close the curtains first"
3. 找到靠垫篮 → INTERACT（拾取） → 去沙发 → INTERACT（放置，靠垫出现在沙发上）

**实现方式**：
- 窗帘 = 窗户位置的不透明平面 USDC（INTERACT 后可见/不可见切换）
- 投影仪 = 架子上的小盒子 USDC
- 靠垫 = 简单几何体，从篮子"转移"到沙发
- 灯光强度变化模拟窗帘效果

**需要的 USDC 资产**：
- 窗帘模型（开/关两个状态 或 动态可见性切换）
- 投影仪小盒子
- 靠垫 / 靠垫篮
- 墙面投影面（发光贴图平面）

**为什么好**：
- **因果依赖**：步骤 2 依赖步骤 1 的完成（projector needs dark room）
- 如果 VLM 跳步，会收到错误反馈，需要重新规划
- 视觉状态变化明显（房间变暗 → 投影出现）

---

### Tier 6：Search & Identify（搜索与识别）

> **难度**：⭐⭐⭐ | **拆解要求**：系统性搜索 → 视觉匹配 → 确认

#### 📋 任务："Find the Broken Appliance / 找到故障电器"
**指令**："One of the appliances in the room is broken — it has a **red warning light** (红色警示灯). Search the room systematically, find the broken appliance, and go stand next to it. The appliances are: **microwave** (on kitchen counter), **desk lamp** (on side table), **TV** (on TV stand), **air purifier** (on floor near bookshelf)."

**VLM 需要拆解**：
1. 制定搜索策略（不能随机乱走，需要系统性扫描）
2. 依次导航到每个电器位置
3. 在每个位置仔细观察 → 判断是否有红色警示灯
4. 找到后 → STOP

**实现方式**：
- 4 个电器 USDC 分布在房间各处
- 随机选一个在旁边放一个红色 SpotLight（模拟警示灯）
- VLM 需要识别颜色异常

**需要的 USDC 资产**：
- 微波炉 / 台灯 / TV / 空气净化器（简单方块 + 贴图即可）
- 红色警示灯 = 红色 SpotLight

**为什么好**：
- **系统性搜索能力**：VLM 需要覆盖整个房间
- **视觉异常检测**：在正常场景中找到异常元素
- 不同 VLM 的搜索策略对比（随机 vs 系统性 → 步数效率差异大）

---

## 技术实现架构

### 1. Proximity Trigger System（靠近触发系统）

```python
# 新增到 vlm_nav_benchmark.py
class InteractionZone:
    """A spatial trigger that fires when the agent enters its radius."""
    def __init__(self, name: str, position: Tuple[float, float], 
                 radius: float, action_callback, prerequisite: str = None):
        self.name = name
        self.position = position
        self.radius = radius
        self.action_callback = action_callback  # 触发时执行的 USD 场景变化
        self.prerequisite = prerequisite          # 前置条件（某个 zone 必须先触发）
        self.triggered = False

INTERACTION_ZONES = [
    InteractionZone("medicine_cabinet", (0.34, 8.76), 1.5, open_cabinet, prerequisite=None),
    InteractionZone("bandage_table", (6.02, 4.50), 1.0, pickup_bandage, prerequisite=None),
    InteractionZone("couch_delivery", (4.37, 6.43), 2.0, deliver_items, prerequisite="medicine_cabinet"),
]
```

### 2. 扩展动作空间

```python
# 现有
ACTION_SPACE = ["MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT", "STOP"]

# 新增
ACTION_SPACE = ["MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT", "STOP", "INTERACT"]
# INTERACT 只在 Agent 处于某个 InteractionZone 内时生效
# 否则提示 "Nothing to interact with here"
```

### 3. Task Specification Format（任务配置格式）

```json
{
  "task_name": "emergency_kit_assembly",
  "task_description": "Collect first-aid supplies and bring them to the injured person.",
  "instruction_to_vlm": "Collect the first-aid supplies: go to the medicine cabinet, pick up the bandage from the table, then bring everything to the injured person on the couch.",
  "interaction_zones": [
    {
      "name": "medicine_cabinet",
      "asset_usdc": "cabinet_with_door.usdc",
      "position": [0.34, 8.76],
      "radius": 1.5,
      "on_interact": "open_door_animation",
      "prerequisite": null
    },
    {
      "name": "bandage_table", 
      "asset_usdc": "bandage_roll.usdc",
      "position": [6.02, 4.50],
      "radius": 1.0,
      "on_interact": "hide_object",
      "prerequisite": null
    },
    {
      "name": "deliver_to_couch",
      "position": [4.37, 6.43],
      "radius": 2.0,
      "on_interact": "show_items_on_couch",
      "prerequisite": ["medicine_cabinet", "bandage_table"]
    }
  ],
  "success_condition": "all_zones_triggered",
  "humans": [
    {"animation": "sitting_idle.usdc", "position": [4.5, 6.5], "role": "injured_person"}
  ],
  "extra_assets": [
    {"usdc": "microwave.usdc", "position": [14.0, 11.5]},
    {"usdc": "air_purifier.usdc", "position": [1.0, 9.0]}
  ]
}
```

### 4. VLM Prompt 变化

当前 prompt 只告诉 VLM "go to TARGET"。新系统需要：

```python
SYSTEM_PROMPT = f"""You are a service robot in a living room. Your task is:
{task_instruction}

You can perform these actions:
- MOVE_FORWARD: move 0.25m forward
- TURN_LEFT / TURN_RIGHT: rotate 15°
- INTERACT: interact with nearby objects (only works when you are close to an interactive object)
- STOP: declare task complete

Think step by step:
1. What sub-goals does this task require?
2. What should I do first?
3. What do I see in the current frame that helps me decide?

Your inventory: {current_inventory}
Completed sub-tasks: {completed_subtasks}
"""
```

### 5. 评价指标扩展

| 指标 | 含义 |
|------|------|
| `success` | 是否完成所有子目标 |
| `steps` | 总步数（效率） |
| `subtask_order_correct` | 子目标完成顺序是否正确（Tier 5 关键） |
| `constraint_violations` | 违反约束次数（如 Tier 3 靠近 sleeper） |
| `search_coverage` | 房间覆盖率（Tier 6 关键） |
| `decomposition_quality` | VLM 输出中是否体现了明确的拆解推理 |

---

## 实现优先级建议

| 优先级 | 任务 | 理由 |
|--------|------|------|
| 🥇 P0 | **Tier 1: Sequential Multi-Stop** | 最小改动即可实现（多个导航目标 + proximity trigger），验证整个框架 |
| 🥈 P1 | **Tier 6: Search & Identify** | 不需要 INTERACT 动作，只需要红色灯 + 多个物体，纯视觉推理 |
| 🥉 P2 | **Tier 2: Conditional Branching** | 需要场景随机化 + 灯光状态切换 |
| 4 | **Tier 3: Social-Aware** | 需要新的人物动画（lying down） |
| 5 | **Tier 5: State-Change Chain** | 需要较多 USD 场景动态变化 |
| 6 | **Tier 4: Multi-Agent** | 需要 3+ 种人物动画 + 动作识别 |

---

## Open Questions

> [!IMPORTANT]
> **Q1: 动画来源**？现在只有 `run` 和 `dance` 两种人物动画。你提到可以不断增加 USDC — 新的人物动画（sitting, waving, sleeping, exercising 等）是通过什么 pipeline 生成的？是 Blender 骨骼动画导出，还是有现成的动画库？这决定了 Tier 3/4/5 的可行性时间表。

> [!IMPORTANT]
> **Q2: INTERACT 动作的呈现**？当 Agent 执行 INTERACT 时，视觉上怎么表现？几个选项：
> - **A**：纯文字反馈（prompt 里告诉 VLM "You picked up the bandage"）+ 物体 visibility 切换
> - **B**：Agent 播放一个 "弯腰/伸手" 的短动画 + 物体消失
> - **C**：Agent 不做动画，但一个 UI overlay 显示 inventory（物品栏）
> 
> 选 A 最简单且不影响视觉真实性，选 B 需要额外的交互动画 USDC。

> [!IMPORTANT]
> **Q3: 你希望先实现哪个任务做 proof-of-concept？** 我建议先做 **Tier 1 (Sequential Multi-Stop)** 或 **Tier 6 (Search & Identify)**，因为改动最小，能最快验证框架。

> [!WARNING]
> **Q4: 场景复用 vs 新场景**？所有任务都在现有 Case 11 客厅场景中做（通过添加资产改造），还是需要切换到 Case 01 办公室 或 全新场景？Case 11 的房间比较大（18m × 12m），空间足够放很多东西。

---

## Verification Plan

### Proof-of-Concept: Tier 1 Sequential Multi-Stop
1. 在现有 `vlm_nav_benchmark.py` 基础上添加 `InteractionZone` 系统
2. 用现有家具作为 3 个交互点（bookshelf → coffee table → sofa）
3. 扩展 VLM prompt 为多步任务指令
4. 运行一次完整 benchmark，观察 VLM 是否能自主拆解并按顺序访问 3 个目标
5. 分析 VLM response 日志中的推理质量

### 测试命令
```bash
ssh GPU-843 "docker exec -e TASK_NAME=sequential_multistop vlm-jupyter /isaac-sim/python.sh \
  /home/qi/hc/Puppeteer/zehao_task/vlm_nav_benchmark.py"
```
