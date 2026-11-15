# 阅读空间双时间维表与事实归属

本项目保存公园阅读空间经营数据格式、原始资料读取程序，以及双时间维表构建与事实重映射。资料中的标识、数量和金额均为合成值。

## 格式与目录

fixtures 保存输入，src/assertion_reader.py 为只读解析入口，src/venue_history.py 负责维表与事实加工，src/examples.py 给出可复现的边界/撤回/乱序样例，tests 核对上述行为。声明按 received_at 获知，所有时间有时区；operation=ASSERT 为声明，RETRACT 为同 assertion_id 的更高修订撤回，空 valid_to 代表无限远。

## 语义

* 业务时间用 valid_from/valid_to，左闭右开；获知时间用 received_at，同一 received_at 是一个发布批次。
* 同一 assertion_id 内 revision 高者替代低者；不同声明重叠时按 (revision, assertion_id) 取较大者，切分为互不重叠的有效片段，无声明覆盖的空洞不产生片段。
* as_of 只使用 received_at <= as_of 的声明，得到该发布批次的维表快照；快照一经发布不随后续撤回/更正改变。
* 事实在 effective_at 上至多匹配一个片段，匹配不到保留原值并记 unknown（missing_venue / no_matching_interval）。
* 代理键 = venue_key + 片段起点 + 分区/门店，不含 valid_to 与批次，因此重跑、撤回、仅截短结尾的更正不会改变已有事实的关联。

## 运行命令

Python 3.11 及以上，只使用标准库，无第三方安装步骤。进入项目根目录运行：

    python -m src.assertion_reader fixtures/assertions.jsonl

构建维表、按批次重映射事实并执行查询：

    python -m src.venue_history \
      --query 'effective_at@2026-09-25T10:00:00+08:00@2026-10-01T00:00:00+08:00' \
      --query 'effective_at@2026-09-25T10:00:00+08:00@2026-10-06T00:00:00+08:00' \
      --query 'timeline@2026-09-25T10:00:00+08:00' \
      --unknown-report .workspace-output/unknown.jsonl \
      --out .workspace-output/report.json

查询语法：effective_at@<业务时间>[@<获知时间>] 按业务时间查某批次已知状态；known_at@<获知时间> 输出整份快照；timeline@<业务时间> 列出该时刻在各批次的认知变化。--as-of 可重复，默认遍历全部批次。

可复现样例（边界、重叠切分、撤回、乱序；两次运行输出逐字节一致）：

    python -m src.examples

编译检查：

    python -m compileall -q src tests

测试：

    python -m unittest discover -s tests -v

## 容器方式

    docker build -t venue-history:local .
    docker run --rm venue-history:local

容器默认执行读取测试。数据加工输出请写入独立路径（如 .workspace-output/），避免覆盖原始资料。

