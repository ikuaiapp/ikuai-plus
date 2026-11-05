# ikuai-plus 🚀

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-iKuai-orange.svg)](https://www.ikuai8.com/)

iKuai 路由功能增强方案：专注于 Docker 环境下的**一机一号**拨号、**多地互联**及企业级实战案例分享。

---

## 📖 项目简介

`ikuai-plus` 旨在挖掘 iKuai 系统的深层潜力，通过Docker插件和数以万计的应用，解决复杂场景下的组网与多协议出口IP难题。

### 核心功能
* **🌐 一机一号 (Per-Container WAN)**：实现在 Docker 容器当独立网关，解决 IP 隔离与业务解耦问题。
* **🔗 多地互联 (Multi-site Connectivity)**：基于 SD-WAN 与 VPN 技术的跨地域内网穿透与打通方案。
* **🛠️ 运维工具箱**：包含环境环境测试等实用 Bash 脚本。
* **📂 经典案例**：汇集真实的生产环境部署架构图与配置清单。

### 部署方案
* **📦 系统镜像化**：提供在官方系统初始化基础上，集成 Docker Web 管理界面的完整磁盘镜像。
* **💾 快速迁移**：通过磁盘压缩技术，实现一键打包分发，支持在云端（ECS/Lighthouse）与本地物理机/虚拟机间快速部署。
* **⚡ 极致部署**：无需重复配置，导入镜像即可获得开箱即用的容器管理环境。

### VPS 重装预览版

[爱快 3.7.16 无 VNC 重装脚本与安全说明](vps_reinstall/README.md)支持单盘 x86-64 Linux VPS，已在腾讯云、RackNerd、云耀和两种 Oracle Cloud 网络配置上测试，包括约 1 GB 内存的 UEFI 实例。镜像与一键脚本见 [多厂商预览版 Release](https://github.com/ikuaiapp/ikuai-plus/releases/tag/vps-reinstall-3.7.16-netboot-pin-20261105)。执行会清空系统盘；请先阅读限制条件并保留云厂商救援入口。`vps_reinstall/vendor/` 中上游救援脚本遵循其自身的 GPL-3.0 许可。
