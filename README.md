# ikuai-plus 🚀

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-iKuai-orange.svg)](https://www.ikuai8.com/)

iKuai 路由功能增强方案：专注于 Docker 环境下的**一机一号**拨号、**多地互联**及企业级实战案例分享。

---

## 📖 项目简介

`ikuai-plus` 旨在挖掘 iKuai 系统的深层潜力，通过自动化脚本与网络架构优化，解决复杂场景下的组网与拨号难题。

### 核心功能
* **🌐 一机一号 (Per-Container PPPoE)**：实现在 Docker 容器内独立进行 PPPoE 拨号，解决 IP 隔离与业务解耦问题。
* **🔗 多地互联 (Multi-site Connectivity)**：基于 SD-WAN 与 VPN 技术的跨地域内网穿透与打通方案。
* **🛠️ 运维工具箱**：包含环境修复、密码找回、磁盘性能优化等实用 Bash 脚本。
* **📂 经典案例**：汇集真实的生产环境部署架构图与配置清单。

---

## 🚀 快速开始

### 1. 环境准备
* 确保 iKuai 系统版本 >= 3.7.x
* 已开启磁盘分区并安装 Docker 插件
* 已开启 SSH 管理权限

### 2. 获取脚本
```bash
# 建议将脚本存放在数据盘路径，防止重启丢失
cd /etc/disk/您的磁盘UUID/Docker/scripts/
git clone [https://github.com/您的用户名/ikuai-plus.git](https://github.com/您的用户名/ikuai-plus.git)
