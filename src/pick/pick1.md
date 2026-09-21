# 重构Picker支持动态候选集合

> **Workspace:** /Users/kkcarrot/swe-project/pick_fork1

---

**User**

现在的Picker 把调用方传入的 Sequence 直接作为实时数据源，却用易漂移的整数 index 和 selected_indexes 表示焦点与选择。选择界面打开期间，只要调用方列表被插入、删除、重排或把当前项改成禁用项，下一轮绘制、说明面板和 get_selected 就可能分别指向不同对象，严重时还会越界；核心选择工作流因而无法安全承载设备发现、任务状态或远端资源等会变化的候选集合。
现在改成给Option 增加显式稳定键，并为普通值定义可预测的兼容身份策略；在 Picker 内引入不可变选项快照和 replace_options/update_options 事件，通过单次状态转换按身份重对账焦点、已选集合、禁用状态、说明内容与当前索引。更新事件应能唤醒正在等待输入的循环，并为当前项删除、选中项删除、重复键和空快照定义策略；旧的静态 Sequence 调用及返回的当前快照索引保持兼容。

**Assistant**

所有现状已清楚。先看一下 mypy 基线，然后开始设计实现。


基线干净。现在开始实现。


核心模块已重写。现在给两个内置后端增加带超时的读取能力。


后端就绪。现在编写覆盖全部新策略的测试。


两个测试细节需要修正：禁用项测试的按键序列需要有 DOWN 键移动焦点；线程唤醒测试的忙等需要小睡以避免计数守卫误触发。


现在运行全部测试。


发现一个真实设计缺陷：仅给 label 的 `Option("b")` 的 `value=None`，导致所有这类 Option 的隐式身份都是 `('value', None)` 而互相冲突，退回位置身份——最常见的 Option 用法反而无法跨重排对账。修正规则：value 未设置时用 label 作为身份。


38 个测试全部通过。运行 mypy 类型检查。


修复 mypy 问题：blessed 后端参数改用 `Any` 免去忽略注释，移除 `__init__.py` 中多余的 ignore。


类型检查与测试全部通过。最后在 README 补充动态选项 API 的精简说明。


重构已完成，38 个测试与 mypy 全部通过（含一个真实多线程唤醒测试，连续 5 次运行稳定）。

## 改动内容

**稳定身份** — [src/pick/__init__.py](file:///Users/kkcarrot/swe-project/pick_fork1/src/pick/__init__.py#L28-L36)
- `Option` 末尾新增 `key: Any = None`，显式稳定键，不破坏旧的位置参数构造。
- 兼容身份策略在 [_build_entries](file:///Users/kkcarrot/swe-project/pick_fork1/src/pick/__init__.py#L91-L139)：显式 key → `("key", k)`；普通值/`Option.value`（未设 value 时用 label）→ `("value", v)`；不可哈希值（dict 等）→ 位置身份 `("index", pos)`。命名空间隔离，不会互相碰撞。

**不可变快照 + 事件** — [Picker](file:///Users/kkcarrot/swe-project/pick_fork1/src/pick/__init__.py#L140-L164)
- 构造时把调用方 Sequence 拷贝为 tuple 快照，调用方之后增删改原列表不再影响打开的菜单。
- [replace_options](file:///Users/kkcarrot/swe-project/pick_fork1/src/pick/__init__.py#L197-L209)：整体替换（允许增删重排）；[update_options](file:///Users/kkcarrot/swe-project/pick_fork1/src/pick/__init__.py#L211-L224)：按身份原地刷新字段、追加新键、不删除。
- 两者可从任意线程调用，经线程安全队列 FIFO 投递到 loop 线程。

**单次状态转换** — [_apply_snapshot](file:///Users/kkcarrot/swe-project/pick_fork1/src/pick/__init__.py#L274-L322)
焦点、已选集合、禁用状态、说明内容、当前索引在同一个原子转换内按身份对账，绘制、说明面板和 `get_selected` 不可能再看到混合状态。

**唤醒等待循环** — [curses_backend.py](file:///Users/kkcarrot/swe-project/pick_fork1/src/pick/curses_backend.py#L39-L50)、[blessed_backend.py](file:///Users/kkcarrot/swe-project/pick_fork1/src/pick/blessed_backend.py#L55-L60)
内置后端新增 `getch_timeout`（curses 用窗口级 timeout 并在用后恢复，blessed 用 `inkey(timeout)`），loop 以 50ms 周期从阻塞中返回处理事件；`Backend` ABC 未加抽象方法，自定义后端零破坏。

**边界策略**
- 当前项删除：优先落在原位置的后继，跳过禁用项并环绕（[test_focus_skips_disabled_successor_after_delete](file:///Users/kkcarrot/swe-project/pick_fork1/tests/test_pick.py#L246-L253)）。
- 选中项删除/变禁用：自动从已选集合剔除，选择顺序保留。
- 当前项变禁用：焦点仍指向同一对象（三者一致），Enter 被 loop 拦截，移动后可正常确认。
- 重复键：显式键重复立即 `ValueError`，不可哈希 key 报 `TypeError`；隐式值重复（如 `["a","a"]`）自动退回位置身份，旧代码照常工作。
- 空快照：运行期合法，`index=-1`，单选 `get_selected()` 为 `(None, -1)`、多选为 `[]`，绘制标题 + `(no options available)`，选项恢复后焦点落首项；构造期空列表仍按旧约定报错。

兼容性方面，旧的静态 Sequence 构造、`pick()`/`__call__`、返回的快照索引与 `(option, index)` / 多选列表形态均保持不变，原 9 个测试原样通过。README 增补了"Changing options while the picker is open"一节。

---

