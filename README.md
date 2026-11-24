# 暴雨断路风险预报与分批保供

这些演练台账是本地合成资料，使用虚构编号。CSV 采用 UTF-8 和带表头的逗号分隔格式。同一单元内用竖线列出的值表示多项或候选集合，含义见各表说明。空值通常表示未记录或不适用，明确的零会保留为 0。

## 资料

- `data/roads.csv`  通行段与车辆可用条件。shared 对应同一条出谷路防护工程，失效时同组受影响。duration 为通车时的小时数。
  字段为 `road / left / right / shared / duration / max_weight`。
- `data/fleet.csv`  车台和窗口资料。day 以一次天气过程开始当日为整数 0；可用窗口是当地钟点。
  字段为 `day / vehicle / open_hour / close_hour / capacity`。
- `data/supplies.csv`  屯点起始盘点、承诺最低量及保质周期。substitute 仅表示按资料可互换的保障用品，充电电源需回库。
  字段为 `site / item / start / minimum / use_per_day / life_days / substitute / return_required`。
- `data/shipments.csv`  在途及落地调度登记。未落地不能当目标屯点存量，已装车物资未回收不能重新拨付。
  字段为 `shipment / case / day / hour / vehicle / item / quantity / to / status / road`。
- `data/notices.csv`  封控通知按业务发生时与可见时间分别记。end_hour 为空表明还未确定结束。closed 对应禁止通行，open 对应正式恢复。
  字段为 `notice / case / road / start_hour / end_hour / known_hour / state`。
- `data/forecasts.csv`  预报版本有自己的到达时间。时间戳为该过程起始后的小时。K001–K030 为历史窗口，K031–K040 为独立复盘，K041–K042 为当前演练。
  字段为 `case / day / run_hour / road / rain / published_hour`。
- `data/patrols.csv`  巡查的时点状态及可见时间。两次巡查中间没有逐小时路况。
  字段为 `case / day / hour / road / state / known_hour`。
- `data/delivery_history.csv`  历史到货记录。未记录表示无确证消息。quantity 为完成卸货的量。
  字段为 `case / day / village / road / quantity / known_hour`。

## 核对台账

环境为 Python 3.11.2，只使用标准库。在仓库根目录运行下列命令检查文件格式、表头和基础外键。输出为各张源表可读行数。

```sh
python3 input_audit.py
python3 -m unittest discover -s tests -v
```

可在第一条命令后指定另一个输入目录，目录中的同名 CSV 沿用上述格式。列名和基础引用清单见 `input_contract.json`。

## 断路风险与滚动保供工具(valleysupply)

县仓可重跑的命令行工具,全部为标准库实现。每个决策时点只使用该时点
**已经送达**的预报版本、封控通知、巡查结果与运输记录;未来日子的路况
只以联合情景分布进入计算,绝不用明天才会看到的实现值安排今天的车。

```sh
python3 -m valleysupply risk    --case K041 --at 4            # 断路风险展望
python3 -m valleysupply plan    --case K041 --at 6 --save     # 编制并登记今日计划
python3 -m valleysupply replan  --case K041 --at 8 --save     # 新通知到达后重算
python3 -m valleysupply review  --score-as-of 40              # 留出案例复盘/复评
python3 -m valleysupply compare --case K041 --at 27           # 路网×货种×时长比较
python3 -m valleysupply trace   --case K041 --village T2      # 按村查
python3 -m valleysupply trace   --case K041 --decision D-K041-001  # 按配送决定查
```

`--at` 为过程起始后的绝对小时;`--save` 把今日决定登记进
`out/state_<case>.json`,下次运行据此还原在途与已落地,已发出的物资
不会在新计划里重复出现。`--alpha`(默认 0.8)控制承诺分位,
`--stance cautious` 改用断路概率区间上界。所有命令可加 `--json`。

### 预测与调拨的关系

- **日尺度断路概率**:按(路段, 雨量等级)统计训练窗(K001–K030)内
  "当日任一有效巡查见断"的经验频率,Beta 平滑并给出 90% 区间。
  巡查只给离散时点状态,两次巡查之间断过路只能按区间删失处理;
  末次巡查仍断的,结束时间超出观察期(右删失),持续期用相邻有效
  巡查对的"今日断、明日仍断"转移频率估计。观察不足的单元格回落到
  全路并集,并集也不足时标记**风险未知**,不给点估计。
- **共模失效**:同一 shared 防护工程的路段用"组冲击+各自冲击"两分量
  结构模拟,同一条出谷路上的多个村同时被切断的联合风险因此可以被
  计算(各村每日孤立概率、多村同时孤立概率、连续孤立天数分布)。
- **空记录**:历史到货的空缺既不是可达证据也不是没货证据(上游一断
  下游就不再回报)。有确证到货只说明该路当日曾可通行;模型不把空缺
  记录当作断路标签。复盘命令会输出这一诊断。
- **调拨**:每日出车窗口内往返,路段限重与车辆限量同时生效;路径可
  途经共用周转点(如经 T2 转 T3)同车分卸。今日放行量按"下次可到达
  日之前的累计缺口"在联合情景上取 alpha 分位;保质按批次先到期先出,
  替代品 1:1 顶用,需回库的电源当天运出的托盘当天收不回,次日回程
  带回、回库充电次日起再发。风险未知的路段不进入承诺,只列出需要的
  人工核实与核实后的可承诺增量。
- **复盘**:参数只用训练案例拟合一次;K031–K040 整段留出。预测在作出
  时落盘(`out/review_predictions.json`),晚到确认到达后用同一份落盘
  预测重新打分(`--score-as-of`),复盘资料不回灌参数。
- **重算线索**:每次计划输出下一次可重算时点(最近的未来预报发布、
  巡查送达、通知送达或出车窗口),以及按村、按配送决定可查的依据
  (所用预报版本、通知、巡查与训练窗)。
