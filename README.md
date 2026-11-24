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
