# 面板在线监测台

一个零第三方依赖（仅 Python 标准库）的网页监控台，集中查看多台服务器的在线状态、CPU / 内存 / 负载 / 磁盘、温度与响应时间，并提供网站/面板的可访问性检测、历史趋势、分组管理、日志、风扇控制（树莓派 GPIO）等。

数据由部署在各服务器上的 **[ysy-agent](https://github.com/Shuying-exquisite/ysy-agent)** 采集器提供，也支持直接读取 1Panel / 宝塔的 API。

## 功能

- 多服务器同屏：左侧服务器小卡（可拖拽排序），点击右侧查看运行详情
- 实时指标：CPU / 内存 / 负载 / 磁盘圆环 + 温度 / 运行时长 / 核心数，3 秒刷新
- 内存口径与 `free` 一致，老内核也准确
- 网站/面板可用性检测、分组管理、拖拽排序
- 历史趋势、可用性统计、响应时间
- 状态变化日志、可选提示音
- 两台监测台之间配置同步
- 可选登录认证与树莓派 GPIO 风扇控制

## 快速开始

### 方式一：Python 直接运行

需要 Python 3.8+，无需安装任何依赖包。

```bash
python3 panel_monitor.py --host 0.0.0.0 --port 8765
```

浏览器打开 `http://<机器IP>:8765`。默认无需登录；在「系统设置」里可开启认证。

### 方式二：Docker

```bash
docker compose up -d
```

访问 `http://<机器IP>:8765`。配置与历史数据持久化在 `./data`。

> 说明：如果你不需要树莓派 GPIO 风扇，可删掉 `docker-compose.yml` 里的 `devices` 段；如需与同机的 ysy-agent 容器互访，保留 `monitor-net` 网络（首次需 `docker network create monitor-net`）。

### 方式三：Windows 可执行文件

用 PyInstaller 打包（`面板在线监测台.spec`），双击 `面板在线监测台.exe` 即可，配置写在同目录 `servers.json`。

## 添加服务器

1. 进入「服务器状态」页，点右上角「添加」。
2. 名称随意填，采集方式选 **Docker 采集器**。
3. API 地址填 `http://<目标机IP>:9100`（采集器装在监测台本机则填 `http://ysy-agent:9100`）。
4. 密钥填采集器的 `AGENT_TOKEN`（没设留空）。
5. 保存后即在左侧列表出现；可拖拽排序、点击查看详情、编辑或删除。

采集器部署见 [ysy-agent 仓库](https://github.com/Shuying-exquisite/ysy-agent)。

## 配置

运行后生成 `servers.json`（可参考 `servers.example.json`）：

- `servers`：网站/面板检测列表（分组、URL、超时等）
- `status_servers`：服务器运行状态列表（名称、采集方式、地址、密钥）
- `listen` / `interval` / `alert_sound` / `auth`：监听、检测间隔、提示音、认证

所有密钥/令牌只存在服务端配置，不随跨端同步传输，也不回显到前端。

## 安全提示

- 若把 8765 端口暴露到公网，务必在设置里开启登录认证，并修改默认凭证
- ysy-agent 的令牌建议使用长随机串，并在防火墙只放行监测台 IP

## License

MIT
