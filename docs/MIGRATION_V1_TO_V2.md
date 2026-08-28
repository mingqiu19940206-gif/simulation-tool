# V1 → V2 配置迁移

1. V2 载入 JSON 时，缺失 `schema_version` 或值为 1 会自动升级为 2。
2. 缺失/无效 `units` 按 `mm` 解释；V1 的全部长度物理值保持不变。
3. 新增组使用安全默认值：`gnd_vias.enabled=false`、`capacitor_model.mode=ideal_bridge`、`simulation.enabled=false`、扫描全部关闭。
4. `capacitor.cap_pitch` 与 `capacitor.pad_pitch` 字段名不变，语义分别固定为 P/N 两颗电容中心距和单颗电容焊盘中心距。
5. 保存后 JSON 会写为 `schema_version=2`，并按所选 `units` 写出全部长度数值。
6. V2 不修改 V1 EXE/ZIP。建议把迁移后的配置另存为新文件，并使用 V2 工程名和 `generated_v2` 输出目录。
7. GND VIA 采用“`drill_diameter` + `plating_thickness` 为输入、`finished_hole_diameter` 自动派生”的唯一关系。载入旧 JSON 时若三者不一致，会按 `drill_diameter - 2 × plating_thickness` 更新成品孔，并在 corrections 中给出原值和新值。
8. `four_corner` 的 `count` 固定归一为 4；`custom` 的 `count` 按 `custom_coordinates` 行数派生。字段名、英文枚举和 V2 JSON 结构保持不变。
9. VIA 关闭时不执行 VIA 专属布局、制造和连接层校验，也不清空原字段；重新启用后仍使用此前保存的值。

迁移不会自动应用新的 0201/0402 建议值，也不会自动打开 VIA 或仿真；这避免旧项目在无确认时改变物理几何。
