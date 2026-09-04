# -*- coding: UTF-8 -*-
"""Purchase-flow helpers for DamaiBot.

Methods relocated from ``mobile/damai_app.py`` (W4-01 split, zero behavior
change).  Hosts the detail→purchase entry path and the fast submit retry loop.
"""

from __future__ import annotations

import logging
import time

from . import (
    _SALE_READY_TEXT_REGEX_OR,
    logger,
)

try:
    from mobile.logger import log_event
except ImportError:  # pragma: no cover
    from logger import log_event  # type: ignore[no-redef]

try:
    from mobile.ui_primitives import ANDROID_UIAUTOMATOR
except ImportError:  # pragma: no cover
    from ui_primitives import ANDROID_UIAUTOMATOR  # type: ignore[no-redef]

try:
    from selenium.webdriver.common.by import By
except ModuleNotFoundError:  # pragma: no cover
    raise


class PurchaseFlowMixin:
    """Mixin contributing detail→purchase entry and submit logic to ``DamaiBot``."""

    _PREFILLED_ACTION_X_RATIO = 0.84
    _PREFILLED_ACTION_Y_RATIO = 0.95
    _PREFILLED_SKU_MAX_CLICKS = 3
    _PREFILLED_SKU_CLICK_INTERVAL_SECONDS = 0.25
    _PREFILLED_SUBMIT_SETTLE_SECONDS = 0.15

    def _cache_prefilled_action_coords(self):
        """Cache one bottom-right hotspot shared by SKU-next and submit."""
        cached = self._cached_hot_path_coords.get("prefilled_action")
        if cached is not None:
            return cached
        if not self._using_u2():
            return None
        try:
            size = self.d.window_size()
            if isinstance(size, dict):
                width, height = int(size["width"]), int(size["height"])
            else:
                width, height = int(size[0]), int(size[1])
            if width <= 0 or height <= 0:
                return None
            coords = (
                min(
                    width - 1,
                    max(0, round(width * self._PREFILLED_ACTION_X_RATIO)),
                ),
                min(
                    height - 1,
                    max(0, round(height * self._PREFILLED_ACTION_Y_RATIO)),
                ),
            )
            self._cached_hot_path_coords["prefilled_action"] = coords
            logger.info("大麦预填直通：已缓存右下角动作坐标 %s", coords)
            return coords
        except Exception:
            logger.warning("大麦预填直通：无法获取屏幕尺寸，不能启用坐标热路径")
            return None

    def _dispatch_prefilled_hotspot_click(self, action, attempt=1):
        """Dispatch one direct u2 coordinate click without a selector lookup."""
        coords = self._cache_prefilled_action_coords()
        if coords is None:
            return False
        dispatched_at_epoch_ms = int(time.time() * 1000)
        click_started_at = time.monotonic()
        try:
            self._click_coordinates(*coords, duration=25)
        except Exception:
            logger.warning("大麦预填直通：右下角动作点击发送失败", exc_info=True)
            return False
        log_event(
            logger,
            f"{action}_dispatched",
            action=action,
            attempt=int(attempt),
            coords=coords,
            dispatched_at_epoch_ms=dispatched_at_epoch_ms,
            dispatch_duration_ms=int((time.monotonic() - click_started_at) * 1000),
        )
        return True

    @staticmethod
    def _activity_is_prefilled_order_confirm(activity):
        """Recognize Damai's native order-confirm activities."""
        if not activity:
            return False
        confirm_markers = (
            "DmOrderActivity",
            "OrderConfirmActivity",
        )
        return any(marker in activity for marker in confirm_markers)

    def _advance_prefilled_sku_to_confirm(self):
        """Click SKU-next up to three times and confirm transition by Activity only."""
        started_at = time.monotonic()
        for attempt in range(1, self._PREFILLED_SKU_MAX_CLICKS + 1):
            if attempt > 1:
                log_event(logger, "sku_next_retry", attempt=attempt)
            dispatch_started_at = time.monotonic()
            if not self._dispatch_prefilled_hotspot_click("sku_next", attempt=attempt):
                return False

            # Keep each attempt on a 250ms cadence.  app_current is the only
            # device read here; no hierarchy, selector, price or attendee query.
            deadline = (
                dispatch_started_at + self._PREFILLED_SKU_CLICK_INTERVAL_SECONDS
            )
            while True:
                activity = self._get_current_activity()
                if self._activity_is_prefilled_order_confirm(activity):
                    log_event(
                        logger,
                        "sku_transition_confirmed",
                        activity=activity,
                        attempts=attempt,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                    )
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.02, remaining))

        logger.warning("大麦预填直通：连续 3 次点击后 Activity 仍停留在 SKU 页")
        return False

    def _submit_prefilled_order_hotspot(self):
        """Submit once at the shared hotspot, then verify without another click."""
        t0 = time.monotonic()
        time.sleep(self._PREFILLED_SUBMIT_SETTLE_SECONDS)
        if not self._dispatch_prefilled_hotspot_click("submit_click", attempt=1):
            log_event(
                logger,
                "order_submitted",
                level=logging.WARNING,
                success=False,
                result="hotspot_unavailable",
                attempts=1,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            return "timeout"

        result = self.verify_order_result(timeout=3)
        if result == "success":
            log_event(
                logger,
                "payment_confirmed",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        log_event(
            logger,
            "order_submitted",
            level=logging.WARNING if result == "timeout" else logging.INFO,
            success=result not in ("timeout", "failed"),
            result=result,
            attempts=1,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return result

    def _enter_purchase_flow_from_detail_page(
        self,
        prepared=False,
        transition_timeout=1.5,
        fallback_probe_on_timeout=True,
    ):
        """Open the purchase panel from the detail page with a low-latency hot path."""
        def wait_for_entry(timeout, poll_interval):
            if fallback_probe_on_timeout:
                return self._wait_for_purchase_entry_result(
                    timeout=timeout, poll_interval=poll_interval
                )
            return self._wait_for_purchase_entry_result(
                timeout=timeout,
                poll_interval=poll_interval,
                fallback_probe_on_timeout=False,
            )

        prefilled_direct_tap = (
            self.config.rush_mode
            and self.config.use_prefilled_selection
            and prepared
        )
        if self.config.rush_mode and not prefilled_direct_tap:
            self._dismiss_fast_blocking_dialogs()
        elif prefilled_direct_tap:
            # 登录/弹窗检查已在开售前完成；到点后第一条
            # 手机指令必须是购票坐标点击，不再串行 5 个弹窗查询。
            logger.info("大麦预填直通：开售后跳过弹窗扫描，直接点击购票")
        if not prepared:
            if self.config.rush_mode:
                if self.config.use_prefilled_selection:
                    logger.info("大麦预填直通：详情页不再重选日期和城市")
                # 极速模式冷路径：单次 XML dump 提取所有坐标（~0.3s），替代多次 _cached_tap（~3-4s）。
                # 预填模式不走该路径，因为它会额外点击日期/城市。
                elif self._using_u2() and not self._cached_hot_path_coords.get(
                    "detail_buy"
                ):
                    # Cold path: single XML dump for all detail page elements.
                    if self._rush_preselect_and_buy_via_xml():
                        next_probe = wait_for_entry(transition_timeout, 0.03)
                        if next_probe is not None and next_probe.get("state") in {
                            "sku_page",
                            "order_confirm_page",
                            "captcha",
                        }:
                            return next_probe
                else:
                    # Warm path: cached coords for date/city/buy.
                    if (
                        self.config.date
                        and "date" not in self._cached_hot_path_no_match
                    ):
                        _date_found = self._cached_tap(
                            "date",
                            ANDROID_UIAUTOMATOR,
                            f'new UiSelector().textContains("{self.config.date}")',
                            timeout=0.1,
                        )
                        if _date_found:
                            logger.info(f"极速模式预选日期: {self.config.date}")
                        elif "date" not in self._cached_hot_path_coords:
                            self._cached_hot_path_no_match.add("date")
                    if (
                        self.config.city
                        and "city" not in self._cached_hot_path_no_match
                    ):
                        _city_found = self._cached_tap(
                            "city",
                            ANDROID_UIAUTOMATOR,
                            f'new UiSelector().text("{self.config.city}")',
                            timeout=0.2,
                        )
                        if not _city_found:
                            _city_found = self._cached_tap(
                                "city",
                                ANDROID_UIAUTOMATOR,
                                f'new UiSelector().textContains("{self.config.city}")',
                                timeout=0.15,
                            )
                        if _city_found:
                            logger.info(f"极速模式预选城市: {self.config.city}")
                        elif "city" not in self._cached_hot_path_coords:
                            self._cached_hot_path_no_match.add("city")
                            logger.debug("极速模式未命中城市选择，继续抢占购票入口")
            else:
                self.select_performance_date()
                logger.info("选择城市...")
                if not self._select_city_from_detail_page(timeout=1.0):
                    logger.warning("城市选择失败")
                    return None

        if not self._cached_hot_path_coords.get("detail_buy"):
            logger.info("点击购票按钮...")
        if self.config.rush_mode:
            # 极速模式：_cached_tap 冷路径查找并缓存购票按钮坐标，热路径直接点击（1次HTTP）。
            # 点击一次后等足够长时间，避免重复点击重置 sku_page 加载。
            had_cached_detail_coords = bool(
                self._cached_hot_path_coords.get("detail_buy")
            )
            detail_click_t0 = time.monotonic()
            _buy_clicked = self._cached_tap(
                "detail_buy",
                By.ID,
                "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl",
                timeout=0.2,
            )
            if not _buy_clicked:
                # 文案集合源 SALE_READY_TEXTS（issue #29）+ 旧文案兜底
                _buy_clicked = self._cached_tap(
                    "detail_buy",
                    ANDROID_UIAUTOMATOR,
                    f'new UiSelector().textMatches("{_SALE_READY_TEXT_REGEX_OR}|.*购票.*|.*抢票.*|.*购买.*")',
                    timeout=0.25,
                )
            if _buy_clicked:
                log_event(
                    logger,
                    "hot_click",
                    action="detail_buy",
                    duration_ms=int((time.monotonic() - detail_click_t0) * 1000),
                    cached=had_cached_detail_coords,
                )
                next_probe = wait_for_entry(transition_timeout, 0.03)
                if next_probe is not None and next_probe.get("state") in {
                    "sku_page",
                    "order_confirm_page",
                    "captcha",
                }:
                    return next_probe

        # 文案集合源 SALE_READY_TEXTS（issue #29）+ 旧"预约/购买"兜底
        book_selectors = [
            (
                By.ID,
                "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl",
            ),
            (
                ANDROID_UIAUTOMATOR,
                f'new UiSelector().textMatches("{_SALE_READY_TEXT_REGEX_OR}|.*预约.*|.*购买.*")',
            ),
            (By.XPATH, '//*[contains(@text,"预约") or contains(@text,"购买")]'),
        ]
        if not self.smart_wait_and_click(
            *book_selectors[0], book_selectors[1:], timeout=0.8
        ):
            logger.warning("购票按钮点击失败")
            return None
        return wait_for_entry(
            transition_timeout if self.config.rush_mode else 5,
            0.05 if self.config.rush_mode else 0.08,
        )

    def _submit_order_fast(self, submit_selectors):
        """Attempt submit quickly and retry within the confirm page before falling back."""
        attempt_count = 3 if self.config.rush_aggressive_retry else 1
        has_submitted_once = False
        t0 = time.monotonic()
        for attempt in range(attempt_count):
            submit_success = False
            # 预填直通不再先「检测提交按钮→丢弃元素→重新
            # 查找点击」；在此一次等待到按钮后立即点击。
            first_timeout = (
                1.5
                if self.config.use_prefilled_selection and attempt == 0
                else 0.35
            )
            click_t0 = time.monotonic()
            if self.ultra_fast_click(*submit_selectors[0], timeout=first_timeout):
                submit_success = True
            elif self.ultra_fast_click(*submit_selectors[1], timeout=0.35):
                submit_success = True
            elif self.smart_wait_and_click(
                *submit_selectors[0], submit_selectors[1:], timeout=0.6
            ):
                submit_success = True

            if not submit_success:
                logger.warning("提交订单按钮未找到，请手动确认订单状态")
                if has_submitted_once:
                    followup_result = self.verify_order_result(timeout=2)
                    if followup_result != "timeout":
                        log_event(
                            logger,
                            "order_submitted",
                            success=followup_result not in ("timeout", "failed"),
                            result=followup_result,
                            attempts=attempt + 1,
                            duration_ms=int((time.monotonic() - t0) * 1000),
                        )
                        return followup_result
                log_event(
                    logger,
                    "order_submitted",
                    level=logging.WARNING,
                    success=False,
                    result="button_not_found",
                    attempts=attempt + 1,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
                return "timeout"

            has_submitted_once = True
            log_event(
                logger,
                "hot_click",
                action="submit_order",
                duration_ms=int((time.monotonic() - click_t0) * 1000),
                attempt=attempt + 1,
            )
            verify_timeout = 1.2 if attempt < attempt_count - 1 else 3
            result = self.verify_order_result(timeout=verify_timeout)
            if result != "timeout":
                log_event(
                    logger,
                    "order_submitted",
                    success=result not in ("timeout", "failed"),
                    result=result,
                    attempts=attempt + 1,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
                return result
            logger.warning(
                f"提交后暂未确认结果，快速重试提交 {attempt + 2}/{attempt_count}"
            )

        log_event(
            logger,
            "order_submitted",
            level=logging.WARNING,
            success=False,
            result="timeout",
            attempts=attempt_count,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return "timeout"
