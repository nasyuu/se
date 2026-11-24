# se · 场景包 Excel IP 回填

## 项目功能

`se` 通过 Linux 跳板机，按网络平面探测场景包 Excel 所需的候选 IPv4 地址。确认分配方案后，程序生成回填副本，保留原文件、已填 IP 和 `N/A`。同组 VIP 共用一个 IP。

## 怎么用

### 1. 安装并准备配置文件

安装 uv 和 Python 3.14，在项目目录执行以下命令。首次使用时复制样例，已有配置可以直接编辑。

```sh
uv sync
cp config/networks.example.yaml config/networks.yaml
cp config/jump_host.example.yaml config/jump_host.yaml
cp .env.example .env
```

### 2. 填写网络配置 `config/networks.yaml`

在 `networks` 下，把样例的网段和地址范围换成本次部署可分配的范围。

| 参数 | 含义和填法 |
| --- | --- |
| `networks` | 按 Excel 所需的网络平面填写。标准平面名为 `om`、`app-om`、`external`、`service`、`service-external`。例如现场使用 `service2` 承载业务面，就在 `service` 下填写它的网段。 |
| `cidr` | 必填。IPv4 网络地址和前缀，例如 `10.70.3.0/24`。各平面网段须互不重叠。 |
| `usable_start` | 必填。候选范围的起始 IP，包含该地址。 |
| `usable_end` | 必填。候选范围的结束 IP，包含该地址。须大于或等于起始 IP，两端都须是该网段内的主机地址。 |
| `gateway` | 可选。填写该网段内的网关 IP，例如 `10.70.3.1`。程序会跳过该地址。 |
| `reserved` | 可选。填写该网段内需要跳过的 IP 列表，例如 `[10.70.3.50, 10.70.3.51]`。默认空列表。 |

[网络配置样例](config/networks.example.yaml)

### 3. 填写跳板机配置 `config/jump_host.yaml`

填写跳板机地址和 SSH 用户名，再为上一步配置的各个平面填写对应网卡。向跳板机管理员获取已核验的 OpenSSH `known_hosts` 条目，保存到 `config/known_hosts`。自定义路径时，将条目保存到 `known_hosts` 字段指向的文件。

| 参数 | 含义和填法 |
| --- | --- |
| `host` | 必填。跳板机的 IP 或域名。 |
| `user` | 必填。SSH 登录用户名。 |
| `interfaces` | 必填。为 `networks` 中每个平面填写不同的跳板机网卡名，例如 `service: eth2`。可在跳板机运行 `ip -br -4 addr` 查看网卡和 IPv4 前缀。对应网卡须有与该平面 `cidr` 一致的 IPv4 网段和前缀。 |
| `port` | 可选。SSH 端口，填 1 到 65535 的整数，默认 `22`。 |
| `known_hosts` | 可选。主机密钥文件路径，用于验证跳板机身份。默认读取 `jump_host.yaml` 同目录下的 `known_hosts`。相对路径以 `jump_host.yaml` 所在目录为基准。 |
| `timeout_seconds` | 可选。SSH 连接和远程命令的超时秒数，填整数，默认 `8`，最大 `60`，须大于 `ping_wait_seconds`。 |
| `ping_wait_seconds` | 可选。单次 Ping 等待回包的秒数，填大于或等于 `1` 的整数，默认 `1`，须小于 `timeout_seconds`。 |

[跳板机配置样例](config/jump_host.example.yaml)

### 4. 填写 SSH 密码 `.env`

打开项目目录下的 `.env`，把 `IPALLOC_SSH_PASSWORD` 的样例值替换成 `user` 对应的 SSH 登录密码。

```dotenv
IPALLOC_SSH_PASSWORD='实际密码'
```

也可以通过同名环境变量提供密码，其值优先于 `.env`。

[密码配置样例](.env.example)

### 5. 准备场景包 Excel

场景包须为 `.xlsx`，包含 `om`、`tenant` 中至少一张工作表，每种最多一张。程序只检查和回填这两种工作表。第 1 行必须有一个 `Node` 表头。两张表共用下列网络表头，可以按实际需要选用，也可以调整列的位置。每个使用的网络表头须与下表完全一致，并且在同一张表中最多出现一次。程序忽略其他列。第 2 行起，只处理 `Node` 列有值的行。

| 网络平面 | 可选表头 |
| --- | --- |
| `om` | `OM-IP`、`OM-VIP` |
| `app-om` | `APP-OM-IP`、`APP-OM-VIP` |
| `service` | `Service-IP`、`Service-VIP` |
| `external` | `External-IP`、`External-VIP` |
| `service-external` | `Service-External-IP` |

例如，`om` 可以只包含 `Node` 和 `Service-IP`，`tenant` 可以只包含 `Node` 和 `OM-VIP`。

- 普通 IP 列和 `om` 的 VIP 列留空即可回填。已分配的地址填 IPv4 文本，填 `N/A` 可跳过。
- `tenant` 的 VIP 列填写 JSON 文本，例如 `{"db": [""]}`。对象的键是组名，值是字符串数组，其中 `""` 表示待填，IPv4 文本表示已有地址。同一 VIP 列里的同名组共用一个地址。整格留空或填 `N/A` 会跳过。

### 6. 检查并回填场景包 Excel

配置填好后，在项目目录执行以下命令，把 `scene.xlsx` 换成实际的场景包 Excel 路径。先运行 `inspect` 检查表头并查看各网络平面的待填槽位，再运行 `apply` 探测 IP。

```sh
uv run se inspect "scene.xlsx"
uv run se apply "scene.xlsx"
```

程序会跳过网关、保留地址、跳板机网卡自身地址、`om` 和 `tenant` 的网络列中已填的地址，以及有 Ping 回包或探测出错的候选地址。候选不足时，终端会列出各平面的缺口并停止回填。

候选齐全后，终端会显示拟回填的单元格和 IP。`no reply` 表示本次 Ping 探测未收到回包，请结合现场已有的 IP 分配核对清单。输入 `y` 后，程序默认在原文件旁生成 `scene_filled.xlsx`。直接回车可取消。

| `apply` 可选参数 | 填法 |
| --- | --- |
| `--config-dir` | 填写同时存放 `networks.yaml` 和 `jump_host.yaml` 的目录，默认为运行目录下的 `config/`。 |
| `--output` | 填写新的 `.xlsx` 输出路径，父目录须已存在。 |
