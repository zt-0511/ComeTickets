# -*- coding: UTF-8 -*-
"""DamaiBot orchestrator: __init__, main run loops, and helper methods that
remain on the bot class after W4-01 split.

Module constants (``SALE_READY_TEXTS`` etc.), the package-level ``logger``
and shared dependency classes are imported from :mod:`mobile.damai_app`'s
``__init__`` (the package).  Hot-path helpers split into mixins live in
sibling submodules: ``sale_waiter``, ``purchase_flow``,
``recovery_strategies`` and ``coords_cache``.
"""

from __future__ import annotations

import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter as _perf_counter

from selenium.webdriver.common.by import By

try:
    from mobile.env_snapshot import detect_damai_app_version
    from mobile.logger import log_event
except ImportError:  # pragma: no cover
    from env_snapshot import detect_damai_app_version  # type: ignore[no-redef]
    from logger import log_event  # type: ignore[no-redef]

from . import (
    ANDROID_UIAUTOMATOR,
    AttendeeSelector,
    BuyButtonGuard,
    Config,
    EventNavigator,
    FastPipeline,
    PageProbe,
    PageState,
    PriceSelector,
    PriceSelectorError,
    RecoveryHelper,
    SessionNotFoundError,
    SoldOutError,
    UIPrimitives,
    logger,
    select_session,
)
from .coords_cache import CoordsCacheMixin
from .delegators import DelegatorsMixin
from .purchase_flow import PurchaseFlowMixin
from .recovery_strategies import RecoveryStrategiesMixin
from .sale_waiter import SaleWaiterMixin
from .state_probe import StateProbeMixin


class DamaiBot(
    SaleWaiterMixin,
    PurchaseFlowMixin,
    RecoveryStrategiesMixin,
    CoordsCacheMixin,
    StateProbeMixin,
    DelegatorsMixin,
    UIPrimitives,
):
    def __init__(self, config=None, setup_driver=True):
        self.config = config or Config.load_config()
        self.item_detail = None
        self.driver = None
        self.d = None
        self.wait = None
        self._terminal_failure_reason = None
        self._last_run_outcome = None
        # U-12：本次 run_with_retry 的总执行轮次（外层尝试与快速重试各 +1），
        # 供运行摘要 attempts 字段消费；纯整数自增，零热路径开销。
        self._attempts_made = 0
        self._last_discovery_step_timings = []
        # 正式抢票各环节耗时；只读本地单调时钟，不会增加手机请求。
        self._purchase_stage_timings = []
        # Cache of {key: (x, y)} coordinates for hot-path elements.
        # Populated on the first run and reused on warm retries (1 HTTP call vs 4+).
        self._cached_hot_path_coords: dict = {}
        # Keys of preselect steps that failed on a previous run and should be skipped.
        self._cached_hot_path_no_match: set = set()
        self._prepare_runtime_config()
        if setup_driver:
            self._setup_driver()

        # Sub-modules that work with any backend (Appium or u2)
        _device = self.d or self.driver
        self._attendee_sel = AttendeeSelector(_device, self.config)
        self._attendee_sel.set_bot(self)
        self._price_sel = PriceSelector(_device, self.config, probe=None)
        self._price_sel.set_bot(self)
        self._navigator = EventNavigator(_device, self.config, probe=None)
        self._navigator.set_bot(self)

        # Sub-modules for u2-optimized operations
        if self.d is not None:
            self._page_probe = PageProbe(self.d, self.config)
            self._page_probe.set_bot(self)
            self._guard = BuyButtonGuard(self.d)
            self._pipeline = FastPipeline(
                self.d, self.config, self._page_probe, self._guard
            )
            self._pipeline.set_bot(self)
            # Share coordinate cache with pipeline
            self._pipeline._cached_coords = self._cached_hot_path_coords
            self._pipeline._cached_no_match = self._cached_hot_path_no_match
            # Update sub-modules with real probe now that it exists
            self._navigator._probe = self._page_probe
            self._recovery = RecoveryHelper(self.d, self._page_probe, self._navigator)
            self._price_sel._probe = self._page_probe

        if setup_driver:
            self._emit_boot_snapshot()

    def _emit_boot_snapshot(self) -> None:
        """Emit a one-shot ``event=boot`` log line with environment metadata."""
        try:
            serial = getattr(self.config, "serial", None) or getattr(
                self.config, "device_serial", None
            )
            damai_version = detect_damai_app_version(serial=serial) or "unknown"
            log_event(
                logger,
                "boot",
                py_version=f"{sys.version_info.major}.{sys.version_info.minor}",
                damai_version=damai_version,
                device=serial or "unknown",
                rush_mode=bool(getattr(self.config, "rush_mode", False)),
            )
        except Exception:  # pragma: no cover — boot logging must never crash startup
            logger.debug("boot snapshot failed (suppressed)", exc_info=True)

    def _ensure_pipeline(self):
        """Lazily create the FastPipeline if not yet initialised (e.g. in tests)."""
        if hasattr(self, "_pipeline"):
            return
        device = self.d or self.driver
        probe = getattr(self, "_page_probe", None)
        guard = getattr(self, "_guard", None)
        self._pipeline = FastPipeline(device, self.config, probe, guard)
        self._pipeline.set_bot(self)
        self._pipeline._cached_coords = self._cached_hot_path_coords
        self._pipeline._cached_no_match = self._cached_hot_path_no_match

    _SOLD_OUT_RE = re.compile(r"缺货|售罄|无票")

    _BUY_BUTTON_NODE_IDS = ("btn_buy_view", "cn.damai:id/btn_buy_view")
    _BUY_BAR_CONTAINER_NODE_IDS = (
        "trade_project_detail_purchase_status_bar_container_fl",
        "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl",
    )

    def _node_shows_sold_out(self, node):
        """判断单个 XML 节点的 text/content-desc 是否包含缺货文案。"""
        text = node.get("text", "")
        desc = node.get("content-desc", "")
        return bool(
            self._SOLD_OUT_RE.search(text) or self._SOLD_OUT_RE.search(desc)
        )

    def _is_buy_button_sold_out(self):
        """Check if the buy button itself shows sold-out text.

        Only inspects the buy button / purchase-bar element, not the whole
        page, to avoid false positives from other price tiers showing '缺货登记'.

        优先匹配 v8.x 的 btn_buy_view（有文案，可判缺货）；仅当其在整棵树中
        不存在时才回退到 ≥9.0.2x 的购买栏容器（issue #41）。注意 iter("node")
        是文档序（父先于子），容器很可能是 btn_buy_view 的祖先，不能在首个
        命中节点上早退，否则 v8.x 的缺货文本会被空文案容器短路漏检。
        容器为 Canvas 自绘（text/content-desc 均空）时返回 False，不误判缺货。
        """
        xml_root = self._dump_hierarchy_xml()
        if xml_root is None:
            return False
        container_node = None
        for node in xml_root.iter("node"):
            rid = node.get("resource-id", "")
            if rid in self._BUY_BUTTON_NODE_IDS:
                return self._node_shows_sold_out(node)
            if container_node is None and rid in self._BUY_BAR_CONTAINER_NODE_IDS:
                container_node = node
        if container_node is not None:
            return self._node_shows_sold_out(container_node)
        return False

    def _set_terminal_failure(self, reason):
        """Mark the current failure as non-retriable."""
        self._terminal_failure_reason = reason

    def _set_run_outcome(self, outcome):
        """Record the terminal outcome for the latest run attempt."""
        self._last_run_outcome = outcome

    def _record_purchase_stage(self, stage, started_at):
        """Append one low-overhead stage duration to logs and run summary."""
        try:
            duration_ms = max(0, int((_perf_counter() - started_at) * 1000))
            item = {
                "attempt": int(self._attempts_made or 1),
                "stage": stage,
                "duration_ms": duration_ms,
            }
            self._purchase_stage_timings.append(item)
            log_event(
                logger,
                "stage_timing",
                attempt=item["attempt"],
                stage=stage,
                duration_ms=duration_ms,
            )
        except Exception:  # pragma: no cover - timing must never affect purchasing
            logger.debug("stage timing failed (suppressed)", exc_info=True)

    @contextmanager
    def _purchase_timed_stage(self, stage):
        """Measure a small purchase block without changing its control flow."""
        started_at = _perf_counter()
        try:
            yield
        finally:
            self._record_purchase_stage(stage, started_at)

    def _report_pending_order_dialog(self):
        """检测到「未支付订单弹窗」时的统一上报（真话优先，U-10 同源）。

        probe_only 从不提交任何订单，此弹窗必然是账号里预先存在的占单（非本次
        安全探测产生）。旧实现无论何种模式都记 ``order_pending_payment`` →
        「抢票成功：检测到未支付订单」，会误导安全探测用户以为 probe 下了单
        （2026-07-11 真机实测）。据实分流，返回 True 语义不变：
        - probe_only：``preexisting_pending_order`` —— 只做探测提醒，不称成功；
        - 正式/开发模式：``order_pending_payment`` —— 本轮流程占单，沿用原语义。
        """
        if self.config.probe_only:
            self._set_run_outcome("preexisting_pending_order")
            logger.info(
                "探测提醒：账号存在未支付订单（非本次安全探测产生），"
                "如需可自行前往订单页处理；本次探测未做任何提交"
            )
        else:
            self._set_run_outcome("order_pending_payment")
            logger.info(
                "检测到未支付订单弹窗（已占单待支付），请立即前往订单页完成支付"
            )
        return True

    def _execution_mode_key(self):
        """Return the current execution mode key."""
        if self.config.probe_only:
            return "probe"
        if not self.config.if_commit_order:
            return "validation"
        return "submit"

    def _execution_mode_label(self):
        """Return a short user-facing label for the current execution mode."""
        labels = {
            "probe": "安全探测",
            "validation": "开发验证",
            "submit": "正式抢票",
        }
        return labels[self._execution_mode_key()]

    def _execution_mode_description(self):
        """Return a user-facing description for the current execution mode."""
        descriptions = {
            "probe": "只检查目标演出页，不会点击“立即购票”",
            "validation": "会继续进入确认页并勾选观演人，但不会点击“立即提交”；这是开发调试路径",
            "submit": "会尝试提交订单",
        }
        return descriptions[self._execution_mode_key()]

    def _log_execution_mode(self):
        """Emit a clear log that tells the user what this run will actually do."""
        logger.info(
            f"开始执行{self._execution_mode_label()}：{self._execution_mode_description()}"
        )

    def _log_success_outcome(self, retry_prefix=""):
        """Emit a success log message that matches the actual run outcome."""
        prefix = f"{retry_prefix}" if retry_prefix else ""
        outcome_messages = {
            "probe_ready": "探测成功：已到目标演出页，购票控件已就绪",
            "validation_ready": "开发验证成功：已到订单确认页，未提交订单",
            "order_submitted": "抢票成功：已提交订单",
            "order_pending_payment": "抢票成功：检测到未支付订单，请立即前往支付完成下单",
            "preexisting_pending_order": "探测提醒：账号存在未支付订单（非本次探测产生），本次未做任何提交",
            "order_flow_completed": "抢票流程完成：已执行提交，等待后续结果确认",
        }
        logger.info(
            f"{prefix}{outcome_messages.get(self._last_run_outcome, '本轮执行成功')}"
        )

    def _finalize_submit_result(self, result, *, start_time=None):
        """_submit_order_fast 结果 → (返回值, outcome/terminal) 的唯一映射。

        主路径 run_ticket_grabbing 与快速重试 order_confirm_page 分支共用，
        保证两条提交路径对同一 verify 结果语义一致（U-10）——
        「成功」只能由 verify_order_result 确认，点击成功不算数。
        耗时信息仅在提供 start_time 时输出（主路径口径）。
        """
        elapsed_suffix = (
            "" if start_time is None else f"耗时: {time.time() - start_time:.2f}秒"
        )
        if result == "success":
            self._set_run_outcome("order_submitted")
            logger.info(f"抢票成功！{elapsed_suffix}")
            return True
        if result == "existing_order":
            self._set_run_outcome("order_pending_payment")
            logger.info(
                f"检测到未支付订单（已占单待支付），请立即前往订单页支付。{elapsed_suffix}"
            )
            return True
        if result == "sold_out":
            # 失败路径也记录真实结局，禁止虚报成功；售罄可保留后续重试语义。
            self._set_run_outcome(result)
            return False
        if result == "captcha":
            self._set_run_outcome(result)
            self._set_terminal_failure("captcha")
            logger.error("检测到大麦验证码，已暂停自动点击，请在手机上手动完成验证")
            return False
        # timeout / 未知值 — fail closed，避免虚假成功与重复提交
        self._set_terminal_failure("submit_unverified")
        logger.error(
            f"提交后未能确认成功状态（result={result}），"
            f"为避免重复下单已停止自动重试，请手动检查订单列表。{elapsed_suffix}"
        )
        return False

    @contextmanager
    def _timed_step(self, step_name, manual_baseline_seconds=None):
        """Record and log per-step latency for discovery hot path."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            faster_than_manual = (
                True
                if manual_baseline_seconds is None
                else elapsed <= manual_baseline_seconds
            )
            self._last_discovery_step_timings.append(
                {
                    "step": step_name,
                    "seconds": round(elapsed, 3),
                    "manual_baseline_seconds": manual_baseline_seconds,
                    "faster_than_manual": faster_than_manual,
                }
            )
            if manual_baseline_seconds is None:
                logger.info(f"步骤耗时[{step_name}] {elapsed:.2f}s")
            else:
                relation = "快于手动基线" if faster_than_manual else "慢于手动基线"
                log_fn = logger.info if faster_than_manual else logger.warning
                log_fn(
                    f"步骤耗时[{step_name}] {elapsed:.2f}s（手动基线 {manual_baseline_seconds:.2f}s，{relation}）"
                )

    def _prepare_runtime_config(self):
        """Pre-flight config checks before creating the driver session."""
        pass

    def _setup_driver(self):
        """初始化 uiautomator2 直连驱动。"""
        import uiautomator2 as u2

        serial = getattr(self.config, "serial", None) or None
        self.d = u2.connect(serial)
        try:
            self.d.settings["wait_timeout"] = 0
            self.d.settings["operation_delay"] = (0, 0)
        except Exception:
            # 测试桩或精简驱动对象可能不支持 dict-style settings
            pass
        should_start_app = True
        try:
            current_app = self.d.app_current()
            if (
                isinstance(current_app, dict)
                and current_app.get("package") == self.config.app_package
            ):
                should_start_app = False
        except Exception:
            should_start_app = True

        if should_start_app:
            self.d.app_start(
                self.config.app_package,
                activity=self.config.app_activity,
                stop=False,
            )
        self.driver = self.d
        self.wait = None

    # Core element operations, click operations, element inspection, and selector
    # utilities are inherited from UIPrimitives (mobile/ui_primitives.py).

    def wait_for_page_state(self, expected_states, timeout=5, poll_interval=0.2):
        """轮询等待页面进入指定状态，返回最后一次探测结果。"""
        deadline = time.time() + timeout
        last_probe = None

        while time.time() < deadline:
            last_probe = self.probe_current_page(fast=True)
            if last_probe["state"] in expected_states:
                return last_probe
            time.sleep(poll_interval)

        return last_probe if last_probe is not None else self.probe_current_page()

    def _wait_for_purchase_entry_result(
        self, timeout=1.2, poll_interval=0.04, fallback_probe_on_timeout=True
    ):
        """Wait for the detail-page CTA to open either sku or confirm page."""
        if self.config.rush_mode:
            # 极速模式：每次轮询只用 1 个 ID 选择器（~60ms/轮），替代 4 个选择器（~240ms/轮）。
            # 大麦 9.0.32 的 SKU 页稳定暴露 NcovSkuActivity，但不一定
            # 暴露旧 layout_sku。预填模式先做单次 Activity RPC，命中即返回。
            skip_reservation_check = not self.config.if_commit_order
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.config.use_prefilled_selection:
                    current_activity = self._get_current_activity()
                    if "NcovSku" in current_activity:
                        return {
                            "state": "sku_page",
                            "price_container": True,
                            # 开售时刻信任用户已完成的大麦预填，
                            # 不再串行 5 个预约文案选择器。
                            "reservation_mode": False,
                        }
                    time.sleep(poll_interval)
                    continue
                if self._has_element(By.ID, "cn.damai:id/layout_sku"):
                    return {
                        "state": "sku_page",
                        "price_container": True,
                        "reservation_mode": False
                        if skip_reservation_check
                        else self.is_reservation_sku_mode(),
                    }
                if self._has_element(By.ID, "cn.damai:id/checkbox"):
                    return {"state": "order_confirm_page", "submit_button": True}
                time.sleep(poll_interval)
            if fallback_probe_on_timeout:
                # fast probe 自带验证码识别；避免先串行 4 个验证码
                # 选择器，随后又做一次完整页面探测。
                return self.probe_current_page(fast=True)
            if self._is_captcha_page():
                return {"state": "captcha", "captcha": True}
            return None

        submit_selectors = [
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("立即提交")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches(".*提交.*|.*确认.*")'),
            (By.XPATH, '//*[contains(@text,"提交")]'),
        ]
        sku_selectors = [
            (By.ID, "cn.damai:id/project_detail_perform_price_flowlayout"),
            (By.ID, "cn.damai:id/layout_sku"),
            (By.ID, "cn.damai:id/sku_contanier"),
            (By.ID, "cn.damai:id/layout_price"),
            (By.ID, "cn.damai:id/tv_price_name"),
        ]

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._has_any_element(submit_selectors):
                return {"state": "order_confirm_page", "submit_button": True}
            if self._has_any_element(sku_selectors):
                return {
                    "state": "sku_page",
                    "price_container": True,
                    "reservation_mode": self.is_reservation_sku_mode(),
                }
            time.sleep(poll_interval)

        if fallback_probe_on_timeout:
            return self.probe_current_page()
        return None

    def _wait_for_submit_ready(self, timeout=1.6, poll_interval=0.04):
        """Wait until the confirm-page submit button appears."""
        if self.config.rush_mode:
            # 极速模式：单 ID 选择器轮询（~60ms/轮 vs 3选择器 ~180ms/轮）。
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self._has_element(By.ID, "cn.damai:id/checkbox"):
                    return True
                time.sleep(poll_interval)
            # 新版确认页可能不再暴露 checkbox ID；只在轻量 ID 轮询超时后
            # 做一次文案兜底，避免每轮多 selector 查询拖慢热路径。
            confirm_fallbacks = [
                (ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("确认购买")'),
                (ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("实名观演人")'),
                (ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("立即提交")'),
            ]
            if self._has_any_element(confirm_fallbacks):
                return True
            if self._is_captcha_page():
                self._set_run_outcome("captcha")
                self._set_terminal_failure("captcha")
                logger.error("检测到大麦验证码，已暂停自动点击，请在手机上手动完成验证")
            return False

        submit_selectors = [
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("立即提交")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches(".*提交.*|.*确认.*")'),
            (By.XPATH, '//*[contains(@text,"提交")]'),
        ]

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._has_any_element(submit_selectors):
                return True
            time.sleep(poll_interval)

        return False

    def _click_sku_buy_button_element(self, burst_count=1):
        """Fallback to an element click for Damai's custom SKU buy button."""
        try:
            buy_button = self._find(By.ID, "cn.damai:id/btn_buy_view")
        except Exception:
            return False

        click_action = getattr(buy_button, "click", None)
        if not callable(click_action):
            return False

        for attempt in range(max(1, int(burst_count))):
            try:
                click_action()
            except Exception:
                if attempt == 0:
                    return False
            if attempt < burst_count - 1:
                time.sleep(0.03)
        return True

    # ------------------------------------------------------------------
    # Failure diagnostics (P1 #31)
    # ------------------------------------------------------------------

    def _save_price_failure_dump(self, reason: str = "") -> Path | None:
        """Persist a UI hierarchy XML (and best-effort screenshot) to tmp/.

        Called when the price selection path fails so post-mortem analysis
        does not need a live device. ``tmp/`` is git-ignored.
        Returns the dump path on success, or ``None`` if nothing could be
        captured (e.g. driver unavailable).
        """
        if self.config.rush_mode and self.config.rush_skip_price_dump:
            logger.debug("rush_skip_price_dump=true，跳过抢票热路径失败截图/XML")
            return None

        try:
            tmp_dir = Path("tmp")
            tmp_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            xml_path = tmp_dir / f"price_dump_{ts}.xml"

            xml_text = ""
            try:
                if self._using_u2():
                    xml_text = self.d.dump_hierarchy()
            except Exception as exc:  # pragma: no cover - logged only
                logger.warning("dump_hierarchy 失败: %s", exc)
                xml_text = ""

            if not xml_text:
                logger.warning("无法获取 UI hierarchy，跳过 dump 落盘")
                return None

            xml_path.write_text(xml_text, encoding="utf-8")
            suffix = f"（reason={reason}）" if reason else ""
            logger.error("failure dump saved to %s%s", xml_path, suffix)

            # best-effort screenshot — never block the dump on screenshot failure
            png_path = xml_path.with_suffix(".png")
            try:
                if self._using_u2() and hasattr(self.d, "screenshot"):
                    self.d.screenshot(str(png_path))
                    logger.error("failure screenshot saved to %s", png_path)
            except Exception as exc:  # pragma: no cover - logged only
                logger.debug("screenshot 不可用: %s", exc)

            return xml_path
        except Exception as exc:  # pragma: no cover - logged only
            logger.warning("保存 price dump 失败: %s", exc)
            return None

    def select_performance_date(self, timeout=1.0):
        """选择演出场次日期"""
        if not self.config.date:
            return

        date_selector = f'new UiSelector().textContains("{self.config.date}")'
        if self.ultra_fast_click(ANDROID_UIAUTOMATOR, date_selector, timeout=timeout):
            logger.info(f"选择场次日期: {self.config.date}")
        else:
            logger.debug(f"未找到日期 '{self.config.date}'，使用默认场次")

    def _handle_session_picker(self):
        """SESSION_PICKER 状态下按 config.date / config.city 选定场次。

        优先级与 :func:`mobile.event_navigator.select_session` 一致：
        date+city > date 唯一 > fallback_index(0)。多场次场景下永不跳过，
        即便 rush_skip_session=True 也强制选场（issue #25 根因防御）。
        """
        try:
            from mobile.date_utils import normalize_date
        except ImportError:  # pragma: no cover
            from date_utils import normalize_date  # type: ignore[no-redef]

        if getattr(self.config, "rush_skip_session", False):
            logger.warning(
                "rush_skip_session=True 但当前为多场次场次选择页 — 强制选场避免抢错（issue #25）"
            )

        normalised_date = normalize_date(self.config.date) if self.config.date else None
        try:
            chosen_idx = select_session(
                self.d,
                date=normalised_date,
                city=self.config.city,
                fallback_index=0,
            )
            logger.info(
                "多场次活动：已选定场次 idx=%d (date=%s, city=%s)",
                chosen_idx,
                normalised_date,
                self.config.city,
            )
            return True
        except SessionNotFoundError as exc:
            logger.error("多场次活动：select_session 失败 — %s", exc)
            self._set_terminal_failure("session_not_found")
            return False

    def _select_city_from_detail_page(self, timeout=1.0):
        """Select the configured city on the detail page."""
        city_selectors = [
            (ANDROID_UIAUTOMATOR, f'new UiSelector().text("{self.config.city}")'),
            (
                ANDROID_UIAUTOMATOR,
                f'new UiSelector().textContains("{self.config.city}")',
            ),
            (By.XPATH, f'//*[@text="{self.config.city}"]'),
        ]
        return self.smart_wait_and_click(
            *city_selectors[0], city_selectors[1:], timeout=timeout
        )

    def _prepare_detail_page_hot_path(self):
        """Preselect detail-page filters before the sale opens so launch-time work is minimized."""
        page_probe = self.probe_current_page()
        if page_probe["state"] != "detail_page":
            return False

        # 用户已在大麦预约页预填场次/票档/观演人时，不再重复点击筛选项；
        # 仅缓存详情页 CTA 坐标，开售后一次点击直达下一步。
        if self.config.use_prefilled_selection:
            guard = getattr(self, "_guard", None)
            coords = guard.get_cta_center_coords() if guard is not None else None
            if coords is not None:
                self._cached_hot_path_coords["detail_buy"] = coords
                logger.info("已启用大麦预填直通：保留预填信息并缓存购票坐标")
                return True
            logger.warning("预填直通未能缓存购票坐标，到点将使用安全兜底路径")
            return False

        prepared = False
        if self.config.date:
            self.select_performance_date()
            prepared = True

        if self.config.city and self._select_city_from_detail_page(timeout=0.6):
            logger.info(f"已预选城市: {self.config.city}")
            prepared = True

        return prepared

    def check_session_valid(self):
        """检查大麦 App 登录状态是否有效"""
        activity = self._get_current_activity()
        if "LoginActivity" in activity or "SignActivity" in activity:
            logger.error("检测到登录页面，大麦 App 登录已过期，请重新登录")
            return False

        login_prompt_selectors = [
            'new UiSelector().textContains("请先登录")',
            'new UiSelector().textContains("登录/注册")',
        ]
        for selector in login_prompt_selectors:
            if self._has_element(ANDROID_UIAUTOMATOR, selector):
                logger.error("检测到登录提示，请重新登录大麦 App")
                return False

        return True

    def verify_order_result(self, timeout=5):
        """验证订单提交结果"""
        start = time.time()
        payment_text_selectors = [
            'new UiSelector().textContains("立即支付")',
            'new UiSelector().textContains("去支付")',
            'new UiSelector().textContains("确认支付")',
            'new UiSelector().textContains("支付剩余时间")',
            'new UiSelector().textContains("收银台")',
        ]

        while time.time() - start < timeout:
            activity = self._get_current_activity()

            # Success: payment page
            if any(kw in activity for kw in ("Pay", "Cashier", "AlipayClient")):
                logger.info("订单提交成功，已跳转支付页面")
                return "success"

            # Check page text for various outcomes
            if self._has_element(
                ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("未支付")'
            ):
                logger.warning("已有未支付订单")
                return "existing_order"
            if (
                self._has_element(
                    ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("已售罄")'
                )
                or self._has_element(
                    ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("库存不足")'
                )
                or self._has_element(
                    ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("暂时无票")'
                )
            ):
                logger.warning("票已售罄")
                return "sold_out"
            if self._has_element(
                ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("滑块")'
            ) or self._has_element(
                ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("验证")'
            ):
                logger.warning("触发验证码")
                return "captcha"
            if any(
                self._has_element(ANDROID_UIAUTOMATOR, selector)
                for selector in payment_text_selectors
            ):
                submit_still_visible = self._has_element(
                    ANDROID_UIAUTOMATOR,
                    'new UiSelector().text("立即提交")',
                )
                confirm_title_visible = self._has_element(
                    ANDROID_UIAUTOMATOR,
                    'new UiSelector().textContains("确认购买")',
                )
                if submit_still_visible or confirm_title_visible:
                    logger.warning(
                        "检测到支付相关文本，但仍在确认购买页，暂不判定提交成功"
                    )
                else:
                    logger.info("订单提交成功，检测到支付页关键控件")
                    return "success"

            time.sleep(0.3)

        logger.warning("订单验证超时")
        return "timeout"

    def dismiss_startup_popups(self):
        """处理首启的一次性系统/应用弹窗。"""
        dismissed = False

        popup_clicks = [
            (By.ID, "android:id/ok"),  # Android 全屏提示
            (By.ID, "cn.damai:id/id_boot_action_agree"),  # 大麦隐私协议
            (By.ID, "cn.damai:id/damai_theme_dialog_cancel_btn"),  # 开启消息通知
            (
                By.ID,
                "cn.damai:id/damai_theme_dialog_close_layout",
            ),  # 新版升级提示关闭按钮
            (By.ID, "cn.damai:id/damai_theme_dialog_close_btn"),  # 新版升级提示关闭图标
            (
                ANDROID_UIAUTOMATOR,
                'new UiSelector().text("Cancel")',
            ),  # Add to home screen
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("下次再说")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("我知道了")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("知道了")'),
        ]

        for by, value in popup_clicks:
            if self._has_element(by, value):
                if self.ultra_fast_click(by, value):
                    dismissed = True
                    time.sleep(0.3)

        return dismissed

    def _dismiss_fast_blocking_dialogs(self):
        """Dismiss lightweight blocking dialogs without the full startup scan."""
        dismissed = False
        dialog_clicks = [
            (By.ID, "cn.damai:id/damai_theme_dialog_cancel_btn"),
            (By.ID, "cn.damai:id/damai_theme_dialog_close_btn"),
            (By.ID, "cn.damai:id/damai_theme_dialog_close_layout"),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("知道了")'),
            (ANDROID_UIAUTOMATOR, 'new UiSelector().text("我知道了")'),
        ]

        for by, value in dialog_clicks:
            if not self._has_element(by, value):
                continue
            if self.ultra_fast_click(by, value, timeout=0.15):
                dismissed = True
                time.sleep(0.05)

        return dismissed

    def run_ticket_grabbing(self, initial_page_probe=None):
        """执行抢票主流程"""
        attempt_started_at = _perf_counter()
        try:
            start_time = time.time()
            self._terminal_failure_reason = None
            self._last_run_outcome = None
            self._log_execution_mode()
            with self._purchase_timed_stage("page_probe"):
                page_probe = initial_page_probe or self.probe_current_page(fast=True)
            fast_validation_hot_path = (
                self.config.rush_mode
                and not self.config.if_commit_order
                and not self.config.use_prefilled_selection
                and initial_page_probe is not None
                and page_probe["state"] in {"detail_page", "sku_page"}
            )
            if fast_validation_hot_path:
                logger.info(
                    "开发验证极速路径：跳过启动弹窗与登录探测，直接执行抢票热路径"
                )
                if page_probe["state"] == "detail_page":
                    if self._has_warm_pipeline_coords():
                        # Warm pipeline: blind shell clicks + concurrent polling.
                        pipeline_result = self._run_warm_validation_pipeline(start_time)
                    elif self._using_u2():
                        # Cold pipeline: XML dump → shell batch → concurrent polling.
                        pipeline_result = self._run_cold_validation_pipeline(start_time)
                    else:
                        pipeline_result = None
                    if pipeline_result is True:
                        return True
                    if pipeline_result is False:
                        return False
                    # pipeline_result is None → fall through to normal flow.
                    # Refresh page_probe since the pipeline may have advanced
                    # the app state (e.g. from detail_page to sku_page).
                    page_probe = self.probe_current_page(fast=True)
            else:
                with self._purchase_timed_stage("startup_and_session_check"):
                    self.dismiss_startup_popups()
                    session_valid = self.check_session_valid()
                if not session_valid:
                    self._set_terminal_failure("session_invalid")
                    return False

            if page_probe["state"] == "pending_order_dialog":
                return self._report_pending_order_dialog()

            if page_probe["state"] == PageState.CAPTCHA.value:
                self._set_run_outcome("captcha")
                self._set_terminal_failure("captcha")
                logger.error("检测到大麦验证码，已暂停自动点击，请在手机上手动完成验证")
                return False

            if page_probe["state"] not in {
                "detail_page",
                "sku_page",
                "order_confirm_page",
                PageState.SESSION_PICKER.value,
            } or (
                self.item_detail and not self._current_page_matches_target(page_probe)
            ):
                if self.config.auto_navigate:
                    logger.info("当前不在目标演出页，尝试自动导航")
                    if not self.navigate_to_target_event(page_probe):
                        return False
                    page_probe = self.probe_current_page()
                    if page_probe["state"] == "pending_order_dialog":
                        return self._report_pending_order_dialog()
                else:
                    logger.warning("当前不在演出详情页，请先手动打开目标演出详情页")
                    return False

            if self.config.probe_only:
                detail_ready = (
                    page_probe["state"] == "detail_page"
                    and page_probe["purchase_button"]
                    and page_probe["price_container"]
                )
                sku_ready = (
                    page_probe["state"] == "sku_page" and page_probe["price_container"]
                )

                if detail_ready or sku_ready:
                    self._set_run_outcome("probe_ready")
                    logger.info(
                        "probe_only 模式: 详情页关键控件已就绪，停止在购票点击前"
                    )
                    end_time = time.time()
                    logger.info(f"探测完成，耗时: {end_time - start_time:.2f}秒")
                    return True

                logger.warning("probe_only 模式: 详情页关键控件未就绪")
                return False

            prepared_detail_page = False
            should_prepare_detail_page = page_probe["state"] == "detail_page" and (
                self.config.sell_start_time is not None
                or (
                    self.config.wait_cta_ready_timeout_ms > 0
                    and not self.config.rush_mode
                )
            )
            if should_prepare_detail_page:
                with self._purchase_timed_stage("pre_sale_prepare"):
                    prepared_detail_page = self._prepare_detail_page_hot_path()
                    page_probe = self.probe_current_page()

            # Wait for sale start if configured
            with self._purchase_timed_stage("wait_for_sale_start"):
                self.wait_for_sale_start()
            # 极速模式沿用开售前已确认的页面状态和缓存坐标，避免到点后再做一次
            # 3~4 秒的完整 hierarchy 探测。非极速模式仍保留稳健重探测。
            if not self.config.rush_mode:
                page_probe = self.probe_current_page()

            if page_probe["state"] == "detail_page":
                with self._purchase_timed_stage("detail_to_purchase"):
                    page_probe = self._enter_purchase_flow_from_detail_page(
                        prepared=prepared_detail_page
                    )
                if page_probe is None:
                    return False
                if page_probe["state"] == PageState.CAPTCHA.value:
                    self._set_run_outcome("captcha")
                    self._set_terminal_failure("captcha")
                    logger.error(
                        "检测到大麦验证码，已暂停自动点击，请在手机上手动完成验证"
                    )
                    return False
            elif page_probe["state"] == "order_confirm_page":
                logger.info("当前已在订单确认页，直接校验观演人和提交状态")
            else:
                logger.info("当前已在票档选择页，跳过城市和预约按钮步骤")
                # 新版 SKU 页会先展示日期卡片，需在此再次选择场次后才会展开票档列表。
                if self.config.use_prefilled_selection:
                    logger.info("大麦预填直通：跳过场次、票档和数量选择，直接下一步")
                elif self.config.rush_mode and not self.config.if_commit_order:
                    logger.info("开发验证极速路径：已在票档页，跳过场次切换")
                else:
                    self.select_performance_date(
                        timeout=0.35 if self.config.rush_mode else 1.0
                    )
                if self.config.rush_mode:
                    # 极速模式下避免一次完整重探测，减少热路径阻塞。
                    page_probe = dict(page_probe)
                    page_probe.setdefault("state", "sku_page")
                    if "reservation_mode" not in page_probe:
                        page_probe["reservation_mode"] = self.is_reservation_sku_mode()
                else:
                    page_probe = self.probe_current_page()

            # 多场次活动：识别 SESSION_PICKER 后强制选场，避免抢错场次（issue #25）。
            if page_probe["state"] == PageState.SESSION_PICKER.value:
                with self._purchase_timed_stage("session_selection"):
                    session_selected = self._handle_session_picker()
                if not session_selected:
                    return False
                page_probe = self.probe_current_page()

            if page_probe["state"] == "sku_page" and page_probe.get("reservation_mode"):
                logger.warning(
                    "检测到当前页面仍是“预售/抢票预约”流程，继续点击底部按钮只会提交预约，不会进入订单确认页"
                )
                self._set_terminal_failure("reservation_only")
                return False

            direct_confirm_ready = page_probe["state"] == "order_confirm_page"

            price_coords = (
                page_probe.get("price_coords") if self.config.rush_mode else None
            )
            buy_button_coords = (
                page_probe.get("buy_button_coords") if self.config.rush_mode else None
            )
            # 热路径优先从 bot 级缓存读取坐标，避免重复 XML dump（热重试节省 ~0.5s）。
            if self.config.rush_mode:
                if price_coords is None:
                    price_coords = self._cached_hot_path_coords.get("price")
                if buy_button_coords is None:
                    buy_button_coords = self._cached_hot_path_coords.get("sku_buy")
            if (
                self.config.rush_mode
                and page_probe["state"] == "sku_page"
                and not self.config.use_prefilled_selection
            ):
                if price_coords is None or buy_button_coords is None:
                    # Single hierarchy dump shared by both coordinate captures (~0.5s vs 4s+).
                    _sku_xml = self._dump_hierarchy_xml()
                    if price_coords is None:
                        price_coords = (
                            self._get_price_option_coordinates_by_config_index(
                                xml_root=_sku_xml
                            )
                        )
                    if buy_button_coords is None:
                        buy_button_coords = self._get_buy_button_coordinates(
                            xml_root=_sku_xml
                        )
            # 更新 bot 级缓存供后续热重试使用。
            if self.config.rush_mode:
                if price_coords is not None:
                    self._cached_hot_path_coords["price"] = price_coords
                if buy_button_coords is not None:
                    self._cached_hot_path_coords["sku_buy"] = buy_button_coords

            # 3. 票价选择 - 优化查找逻辑
            skip_price_selection = (
                direct_confirm_ready
                or self.config.use_prefilled_selection
                or (
                    self.config.rush_mode
                    and not self.config.if_commit_order
                    and self._has_element(By.ID, "layout_num")
                )
            )
            if skip_price_selection:
                if direct_confirm_ready:
                    logger.info("已直接进入订单确认页，跳过票档点击")
                elif self.config.use_prefilled_selection:
                    logger.info("大麦预填直通：使用已预填票档")
                else:
                    logger.info("开发验证极速路径：检测到已处于可调数量状态，跳过票档点击")
            else:
                logger.info("选择票价...")
                try:
                    with self._purchase_timed_stage("price_selection"):
                        selected = self._select_price_option(cached_coords=price_coords)
                except SoldOutError as exc:
                    self._save_price_failure_dump(reason=f"sold_out: {exc}")
                    logger.error("票档已售罄，停止本轮抢票: %s", exc)
                    return False
                except PriceSelectorError as exc:
                    self._save_price_failure_dump(reason=str(exc))
                    logger.error("价格选择失败: %s", exc)
                    return False
                if not selected:
                    self._save_price_failure_dump(reason="select returned False")
                    return False

            # 4. 数量选择
            logger.info("选择数量...")
            if direct_confirm_ready:
                logger.info("已直接进入订单确认页，跳过数量选择")
            elif self.config.use_prefilled_selection:
                logger.info("大麦预填直通：使用已预填数量")
            elif len(self.config.users) > 1 and self._has_element(By.ID, "layout_num"):
                clicks_needed = len(self.config.users) - 1
                if clicks_needed > 0:
                    try:
                        plus_button = self._find(By.ID, "img_jia")
                        for i in range(clicks_needed):
                            rect = self._element_rect(plus_button)
                            x = rect["x"] + rect["width"] // 2
                            y = rect["y"] + rect["height"] // 2
                            self._click_coordinates(x, y, duration=50)
                            time.sleep(0.02)
                    except Exception as e:
                        logger.error(f"快速点击加号失败: {e}")

            # if self.driver.find_elements(by=By.ID, value='layout_num') and self.config.users is not None:
            #     for i in range(len(self.config.users) - 1):
            #         self.driver.find_element(by=By.ID, value='img_jia').click()

            # 5. 确定购买 — brief wait for price selection to register.
            # Damai App ignores confirm clicks until btn_buy_view becomes clickable (price > 0).
            confirm_started_at = _perf_counter()
            purchase_transition_stage = (
                "sku_next_click"
                if self.config.use_prefilled_selection
                and self.config.if_commit_order
                else "purchase_to_confirm"
            )
            submit_ready = direct_confirm_ready
            if submit_ready:
                logger.info("订单确认页已就绪，无需再次点击下一步")
            else:
                time.sleep(0.05 if self.config.use_prefilled_selection else 0.2)
                logger.info("确定购买...")
            confirm_window = (
                1.8
                if self.config.use_prefilled_selection
                else (3.0 if self.config.rush_mode else 1.8)
            )
            confirm_deadline = time.time() + confirm_window
            max_confirm_attempts = (
                1
                if self.config.use_prefilled_selection
                else (4 if self.config.rush_aggressive_retry else 2)
            )
            confirm_attempt = 0
            while (
                time.time() < confirm_deadline
                and not submit_ready
                and confirm_attempt < max_confirm_attempts
            ):
                confirm_attempt += 1
                burst_count = (
                    2
                    if self.config.rush_aggressive_retry
                    and self.config.if_commit_order
                    and not self.config.use_prefilled_selection
                    else 1
                )
                if self.config.rush_mode and buy_button_coords:
                    if confirm_attempt == 1:
                        self._burst_click_coordinates(
                            *buy_button_coords,
                            count=burst_count,
                            interval_ms=25,
                            duration=25,
                        )
                    else:
                        self._click_sku_buy_button_element(
                            burst_count=burst_count
                        )
                elif self.config.rush_mode:
                    # 先做一次轻量元素点击；仅在元素路径失败时解析坐标兜底。
                    clicked = self._click_sku_buy_button_element(
                        burst_count=burst_count
                    )
                    if not clicked:
                        _buy_coords = self._get_buy_button_coordinates()
                        if _buy_coords:
                            self._burst_click_coordinates(
                                *_buy_coords,
                                count=burst_count,
                                interval_ms=25,
                                duration=25,
                            )
                            buy_button_coords = _buy_coords
                        else:
                            self.ultra_fast_click(
                                By.ID, "cn.damai:id/btn_buy_view", timeout=0.2
                            )
                else:
                    if not self.ultra_fast_click(By.ID, "cn.damai:id/btn_buy_view"):
                        self.ultra_fast_click(
                            ANDROID_UIAUTOMATOR,
                            'new UiSelector().textMatches(".*确定.*|.*购买.*")',
                        )
                log_event(
                    logger,
                    "hot_click",
                    action="sku_next",
                    duration_ms=int((_perf_counter() - confirm_started_at) * 1000),
                    prefilled=bool(self.config.use_prefilled_selection),
                )
                if (
                    self.config.use_prefilled_selection
                    and self.config.if_commit_order
                ):
                    # 正式预填模式不在这里等 checkbox/文案后又重新
                    # 查找「立即提交」。_submit_order_fast 会用同一次
                    # wait-and-click 在按钮出现的瞬间直接点击。
                    submit_ready = True
                    logger.info("大麦预填直通：下一步后直接等待并点击立即提交")
                else:
                    # Short wait then check if confirm page appeared
                    remaining = confirm_deadline - time.time()
                    check_timeout = min(
                        1.5 if self.config.use_prefilled_selection else 0.8,
                        max(0.1, remaining),
                    )
                    submit_ready = self._wait_for_submit_ready(
                        timeout=check_timeout,
                        poll_interval=0.03 if self.config.rush_mode else 0.05,
                    )
                if self._terminal_failure_reason:
                    break
                if not submit_ready and confirm_attempt < max_confirm_attempts:
                    logger.debug(f"确定按钮第 {confirm_attempt} 次点击未生效，重试...")
                    time.sleep(0.25)
                # 兜底检查票档是否已经售罄。
                if not submit_ready and confirm_attempt == max_confirm_attempts:
                    if self._is_buy_button_sold_out():
                        logger.warning(
                            "购买按钮区域检测到缺货/售罄标识，该票档当前不可购买"
                        )
                        self._set_terminal_failure("sold_out")
                        self._record_purchase_stage(
                            purchase_transition_stage, confirm_started_at
                        )
                        return False
            self._record_purchase_stage(
                purchase_transition_stage, confirm_started_at
            )
            if self._terminal_failure_reason:
                return False
            if not submit_ready:
                if self.config.use_prefilled_selection:
                    logger.warning("大麦预填直通未进入订单确认页，停止重复点击")
                    return False
                if self.config.rush_mode and not self.config.if_commit_order:
                    logger.info(
                        "开发验证极速路径：确认页未完全就绪，跳过预选用户兜底，直接校验观演人区域"
                    )
                else:
                    # 6. 批量选择用户
                    logger.info("选择用户...")
                    user_clicks = [
                        (ANDROID_UIAUTOMATOR, f'new UiSelector().text("{user}")')
                        for user in self.config.users
                    ]
                    user_timeout = 0.35 if self.config.rush_mode else 1.0
                    clicked_users = self.ultra_batch_click(
                        user_clicks, timeout=user_timeout
                    )
                    if clicked_users:
                        submit_ready = self._wait_for_submit_ready(
                            timeout=0.9 if self.config.rush_mode else 1.5,
                            poll_interval=0.03 if self.config.rush_mode else 0.05,
                        )

            if not submit_ready and not (
                self.config.rush_mode and not self.config.if_commit_order
            ):
                logger.warning("未进入订单确认页，请检查票档可用性或观演人配置")
                return False

            submit_selectors = [
                (ANDROID_UIAUTOMATOR, 'new UiSelector().text("立即提交")'),
                (
                    ANDROID_UIAUTOMATOR,
                    'new UiSelector().textMatches(".*提交.*|.*确认.*")',
                ),
                (By.XPATH, '//*[contains(@text,"提交")]'),
            ]
            if self.config.use_prefilled_selection:
                # 用户明确选择信任大麦预填观演人；不读 checkbox、
                # 不 dump hierarchy，也不做姓名匹配。
                attendees_ready = True
                logger.info("大麦预填直通：按配置跳过观演人状态校验")
                skipped_at = _perf_counter()
                self._record_purchase_stage(
                    "attendee_validation_skipped", skipped_at
                )
            else:
                with self._purchase_timed_stage("attendee_validation"):
                    attendees_ready = self._ensure_attendees_selected_on_confirm_page(
                        require_attendee_section=self.config.rush_mode
                        and not self.config.if_commit_order
                    )
            if not attendees_ready:
                self._set_terminal_failure("attendee_unselected")
                logger.error("订单提交前观演人未选择完整，已停止自动提交")
                return False

            if not self.config.if_commit_order:
                self._set_run_outcome("validation_ready")
                end_time = time.time()
                logger.info(
                    "if_commit_order=False，已完成观演人勾选，停止在“立即提交”前"
                )
                logger.info(
                    f"已到订单确认页且观演人已勾选，未提交订单（开发验证），耗时: {end_time - start_time:.2f}秒"
                )
                return True

            # 7. 提交订单
            logger.info("提交订单...")
            with self._purchase_timed_stage("submit_and_verify"):
                result = self._submit_order_fast(submit_selectors)
            return self._finalize_submit_result(result, start_time=start_time)

        except Exception as e:
            logger.error(f"抢票过程发生错误: {e}")
            try:
                from .recovery_strategies import capture_failure_artifacts

                capture_failure_artifacts(self, "run_ticket_grabbing", error=e)
            except Exception:  # pragma: no cover — failure capture must not crash
                logger.debug("failure artifact capture failed", exc_info=True)
            return False
        finally:
            self._record_purchase_stage("attempt_total", attempt_started_at)
            time.sleep(0.05)

    def run_with_retry(self, max_retries=3, initial_page_probe=None):
        """带重试机制的抢票"""
        self._attempts_made = 0
        self._purchase_stage_timings = []
        for attempt in range(max_retries):
            self._attempts_made += 1
            logger.info(f"第 {attempt + 1} 次尝试（{self._execution_mode_label()}）...")
            # Pass initial_page_probe only on the first attempt; retries must re-probe.
            probe = initial_page_probe if attempt == 0 else None
            if self.run_ticket_grabbing(initial_page_probe=probe):
                self._log_success_outcome()
                return True

            if self._terminal_failure_reason:
                logger.error(
                    f"检测到不可重试失败，停止后续重试: {self._terminal_failure_reason}"
                )
                break

            # Fast retry within same session
            for fast_attempt in range(self.config.fast_retry_count):
                self._attempts_made += 1
                logger.info(
                    f"快速重试 {fast_attempt + 1}/{self.config.fast_retry_count}"
                    f"（{self._execution_mode_label()}）..."
                )
                if fast_attempt > 0 and self.config.fast_retry_interval_ms > 0:
                    time.sleep(self.config.fast_retry_interval_ms / 1000)
                with self._purchase_timed_stage("fast_retry"):
                    fast_retry_succeeded = self._fast_retry_from_current_state()
                if fast_retry_succeeded:
                    self._log_success_outcome("快速重试成功：")
                    return True
                if self._terminal_failure_reason:
                    logger.error(
                        f"快速重试遇到不可重试失败，停止后续重试: {self._terminal_failure_reason}"
                    )
                    break

            if self._terminal_failure_reason:
                break

            # Full driver recreation
            logger.warning(f"第 {attempt + 1} 次尝试及快速重试均失败")
            if attempt < max_retries - 1:
                if not self.config.auto_navigate:
                    logger.info("手动起跑模式，保留当前会话并继续本地重试")
                    continue
                logger.info("重建驱动后重试...")
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self._setup_driver()

        logger.error("所有尝试均失败")
        return False
