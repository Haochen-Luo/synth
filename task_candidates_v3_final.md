# V5: 类"烧开水"任务设计 — 短指令 + 唯一固定步骤

## 设计原则

像"烧开水"一样：指令极短，但人人都知道固定步骤，顺序不可跳过，每步对应一个位置。

---

## ★★ 2-Step Tasks（A → B）

### "Water the plant."（浇花）
- **步骤**：浇水壶（水槽旁）→ INTERACT → 花盆 → INTERACT
- **顺序**：必须先拿水再浇（空手不能浇）
- **资产**：浇水壶 USDC、花盆 USDC

### "Take out the trash."（倒垃圾）
- **步骤**：垃圾袋（厨房垃圾桶旁）→ INTERACT → 门口 → INTERACT
- **顺序**：必须先拿袋子再出门
- **资产**：垃圾桶+袋 USDC、门 USDC

### "Do the dishes."（洗碗）
- **步骤**：脏盘子（餐桌上）→ INTERACT → 水槽 → INTERACT
- **顺序**：先收盘子再拿去洗
- **资产**：脏盘子 USDC ×2、水槽 USDC

### "Hang up the coat."（挂外套）
- **步骤**：外套（搭在椅背上）→ INTERACT → 衣帽架（门旁）→ INTERACT
- **顺序**：先拿外套再挂
- **资产**：外套 USDC、衣帽架 USDC

### "Charge the phone."（给手机充电）
- **步骤**：手机（沙发上）→ INTERACT → 充电器/插座（书桌旁）→ INTERACT
- **顺序**：先拿手机再插充电
- **资产**：手机 USDC、充电线+插座 USDC

### "Mop up the spill."（擦掉洒的水）
- **步骤**：拖把（墙角）→ INTERACT → 地上水渍（蓝色半透明圆）→ INTERACT
- **顺序**：先拿拖把再擦
- **资产**：拖把 USDC、水渍平面 USDC

### "Feed the cat."（喂猫）
- **步骤**：猫粮袋（厨房台面）→ INTERACT → 猫碗（猫旁边）→ INTERACT
- **顺序**：先拿粮再倒
- **资产**：猫粮袋 USDC、猫 USDC、猫碗 USDC

### "Bring medicine to the sick person."（给病人送药）
- **步骤**：药瓶（书架/柜子上）→ INTERACT → 沙发上的病人 → INTERACT
- **顺序**：先拿药再送
- **资产**：药瓶 USDC、坐姿人物动画

### "Put the groceries in the fridge."（把菜放冰箱）
- **步骤**：购物袋（门口地面）→ INTERACT → 冰箱 → INTERACT
- **顺序**：先拿袋子再放
- **资产**：购物袋 USDC、冰箱 USDC

### "Serve tea to the guest."（给客人端茶）
- **步骤**：茶杯（厨房台面/茶几）→ INTERACT → 坐着的客人 → INTERACT
- **顺序**：先端茶再送
- **资产**：茶杯 USDC、坐姿人物动画

---

## ★★★ 3-Step Tasks（A → B → C）

### "Make coffee."（泡咖啡）
- **步骤**：杯子（柜子里/架上）→ INTERACT → 咖啡机 → INTERACT → 桌子 → INTERACT
- **顺序**：先拿杯 → 接咖啡 → 放桌上
- **资产**：杯子 USDC、咖啡机 USDC

### "Warm up the leftovers."（热剩饭）
- **步骤**：饭盒（冰箱旁）→ INTERACT → 微波炉 → INTERACT → 餐桌 → INTERACT
- **顺序**：先拿饭 → 放微波炉 → 端到桌上
- **资产**：饭盒 USDC、微波炉 USDC

### "Mail the letter."（寄信）
- **步骤**：信件（书桌上）→ INTERACT → 信封/邮票（抽屉柜旁）→ INTERACT → 门口 → INTERACT
- **顺序**：拿信 → 装信封 → 送到门口
- **资产**：信件 USDC、信封 USDC、门 USDC

### "Return the book to the shelf."（把书放回书架）
- **步骤**：书（沙发上/茶几上）→ INTERACT → 书架 → INTERACT
- 但如果书掉在地上：地面的书 → INTERACT → 书架 → INTERACT
- **顺序**：先捡书再放
- **资产**：书 USDC、书架（已有）

### "Give the dog its medicine."（给狗喂药）
- **步骤**：药（柜子上）→ INTERACT → 狗粮/零食（厨房）→ INTERACT（藏药进去）→ 狗 → INTERACT
- **顺序**：拿药 → 裹食物里 → 喂狗
- **资产**：药片 USDC、狗零食 USDC、狗 USDC

---

## ★★★★ 4-Step Tasks（A → B → C → D）

### "Cook instant noodles."（煮泡面）
- **步骤**：泡面包装（柜子）→ INTERACT → 锅（厨房台面）→ INTERACT → 灶台 → INTERACT（开火煮）→ 碗（架子）→ INTERACT（盛出来）
- **顺序**：拿面 → 拿锅 → 放灶上 → 盛碗里
- **资产**：泡面 USDC、锅 USDC、灶台 USDC、碗 USDC

### "Wrap a gift."（包礼物）
- **步骤**：礼物（桌上的盒子）→ INTERACT → 包装纸（柜子/抽屉旁）→ INTERACT → 胶带/剪刀（书桌）→ INTERACT → 门口 → INTERACT（准备送出）
- **顺序**：拿礼物 → 拿纸 → 拿工具 → 包好放门口
- **资产**：盒子 USDC、包装纸卷 USDC、剪刀 USDC

---

## 为什么这些比之前好

| 之前的问题 | 这一版 |
|-----------|--------|
| "Set up meeting" — 什么叫准备好？不唯一 | "泡咖啡" — 杯子→咖啡机→桌子，人人一样 |
| "Tidy up" — 每个人标准不同 | "洗碗" — 脏盘子→水槽，没有歧义 |
| "Make comfortable" — 无正确答案 | "挂外套" — 外套→衣帽架，唯一 |
| "The TV is still on" — 没有任务动词 | "喂猫" — 猫粮→猫碗，明确动作 |

**共同特征**：
- 指令 ≤ 5 个词
- 全人类共识的固定步骤
- 步骤顺序不可跳过（先拿X才能做Y）
- 每步 = 一个位置 + INTERACT
- 成功标准：按正确顺序触发所有 InteractionZone

---

## 资产需求（给合作伙伴）

### P0 — 覆盖最多任务的高复用资产

| 资产 | 复用于 |
|------|--------|
| 门 | 倒垃圾、寄信 |
| 猫/狗 | 喂猫、给狗喂药 |
| 手机 | 充手机 |
| 杯子/茶杯 | 端茶、泡咖啡 |
| 水槽 | 洗碗、浇花 |

### P1 — 单任务专用

| 资产 | 任务 |
|------|------|
| 浇水壶 + 花盆 | 浇花 |
| 拖把 | 擦地 |
| 垃圾桶+袋 | 倒垃圾 |
| 衣帽架 + 外套 | 挂外套 |
| 微波炉 | 热剩饭 |
| 咖啡机 | 泡咖啡 |
| 猫碗 + 猫粮袋 | 喂猫 |
| 药瓶 | 送药、给狗喂药 |

### 人物动画

| 动画 | 任务 |
|------|------|
| 坐姿 sitting | 端茶给客人、送药给病人 |
| 倒地（旋转现有 runner） | 之前的 help injured（如保留） |

### 关键问题
> **Q1**: 静态物体 USDC 来源？Objaverse 下载→转 USDC？Blender 建模？
> **Q2**: 能否 `stage.DefinePrim()` 动态加载新 USDC？（和现在加 agent 一样）
> **Q3**: sitting 人物动画能生成吗？
