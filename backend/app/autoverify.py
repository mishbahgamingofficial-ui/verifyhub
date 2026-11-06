"""Automatic verification against the registration site's agent data.

Mishbah's manual check is exactly two things: (1) the UID exists,
(2) how much the user deposited (more than Rs 300). Both live in the
site's Promotion section (the agent/downline panel). This module
automates that check against the site's own account API — the same
API the site's frontend uses — so a clean pass can verify a
submission without an admin touching it.

How it works (all mapped from the live site's own JavaScript, not
guessed):
- API host is configurable (``AUTOVERIFY_BASE_URL``): the DmFirst
  brand lives at ``https://api.dmfirst9api.com``, the DmPlay brand at
  ``https://api.dmplay9api.com``. Prefix is ``/api/webapi``, every
  call is a POST with the same plain-MD5 signing as the results API
  (see ``app.prediction.build_signed_body``).
- ``Login`` takes ``{numberType, pwd, logintype: "mobile"}`` where
  ``numberType`` is the dial code + phone number (e.g. ``91`` +
  ``9204133258``). The response carries ``{token, tokenHeader,
  refreshToken}``; later calls send
  ``Authorization: <tokenHeader><token>`` (refresh calls use the
  refresh token instead). ``RefreshToken`` takes an empty body.
- ``GetTeamDayReport`` (path ``/TeamDayReport``) is the Subordinate
  data list: params ``{day: "YYYY-MM-DD 00:00:00", lv, userId,
  pageNo, pageSize}``; each row carries ``userID`` and
  ``rechargeAmount`` (that day's deposit). ``day`` is a single date
  and the site only publishes up to yesterday ("Data is updated
  daily"), so the check sums the last N days (default 14) instead of
  trusting one day, and a same-day deposit can only be verified the
  next day.

Fail-closed by design: any provider error, unexpected payload, or
missing configuration leaves the submission in the admin queue —
nothing here ever auto-rejects, and a failed check is retried the
next day by the daily loop (see ``app.main``). The system actor in
review history is telegram id 0, so an automatic decision is never
misattributed to a human admin.

Credentials come from the environment (``AUTOVERIFY_USERNAME`` /
``AUTOVERIFY_PASSWORD``) — never from chat, never logged, never
stored in the database. Only the check *outcome* (UID found, deposit
total, days checked) is recorded on the submission.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from .db import Submission
from .prediction import DMFIRST_USER_AGENT, build_signed_body
from .services import change_submission_status

logger = logging.getLogger("verifyhub.autoverify")

# The system actor recorded in review history / audit for automatic
# decisions. 0 is not a Telegram id, so it can never be confused with
# a human admin.
SYSTEM_ADMIN_ID = 0

# DmFirst/DmPlay account API (same family as the results API).
API_PREFIX = "/api/webapi"
AUTOVERIFY_TIMEOUT_SECONDS = 8.0
AUTOVERIFY_MAX_ATTEMPTS = 3
# Default lookback: the site only shows one day at a time and data lags
# a day, so a deposit is only visible the day after it happens.
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_MIN_DEPOSIT = 300.0


class AutoVerifyError(Exception):
    """The provider call failed (network, bad payload, error code)."""


def _day_string(day: date) -> str:
    return day.strftime("%Y-%m-%d 00:00:00")


class DmPlayClient:
    """Synchronous client for the site's logged-in account API.

    ``client`` is injectable (httpx.Client, e.g. with a MockTransport)
    for tests; otherwise a short-lived client is built per instance.
    The session token is held in memory only — never written anywhere.
    """

    def __init__(self, base_url: str, origin: str, client: httpx.Client | None = None,
                 timeout: float = AUTOVERIFY_TIMEOUT_SECONDS,
                 max_attempts: int = AUTOVERIFY_MAX_ATTEMPTS):
        self.base_url = base_url.rstrip("/")
        self.origin = origin.rstrip("/")
        self._client = client
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._token: str = ""
        self._token_header: str = ""
        self._refresh_token: str = ""

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _http_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._timeout,
                headers={"User-Agent": DMFIRST_USER_AGENT},
            )
        return self._client

    def _headers(self, use_refresh_token: bool = False) -> dict:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "X-App-Type": "web",
            "Ar-Origin": self.origin,
        }
        token = self._refresh_token if use_refresh_token else self._token
        if self._token_header and token:
            headers["Authorization"] = self._token_header + token
        return headers

    def _post(self, endpoint: str, params: dict, use_refresh_token: bool = False) -> dict:
        """One signed POST. Retries transport errors; on a non-zero
        provider code it refreshes the session once and retries, then
        fails with the provider's own code/message."""
        url = f"{self.base_url}{API_PREFIX}/{endpoint}"
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                body = build_signed_body(dict(params))
                resp = self._http_client().post(url, json=body, headers=self._headers(use_refresh_token))
                data = resp.json()
                if not isinstance(data, dict):
                    raise AutoVerifyError(f"{endpoint} returned a non-object payload.")
                if data.get("code") != 0:
                    code = data.get("code")
                    msg = data.get("msg") or ""
                    # A stale session is the one failure worth healing:
                    # refresh once (only on the first attempt, and never
                    # for the refresh call itself) and retry.
                    if not use_refresh_token and self._refresh_token and attempt == 0:
                        try:
                            self.refresh()
                        except AutoVerifyError:
                            raise AutoVerifyError(
                                f"{endpoint} failed: code {code}: {msg}"
                            ) from None
                        continue
                    raise AutoVerifyError(f"{endpoint} failed: code {code}: {msg}")
                result = data.get("data")
                return result if isinstance(result, dict) else {}
            except AutoVerifyError:
                raise
            except (httpx.TransportError, ValueError) as exc:
                last_error = exc
                logger.warning("Auto-verify %s attempt %s failed: %s", endpoint, attempt + 1, exc)
                if attempt < self._max_attempts - 1:
                    time.sleep(0.2 * (attempt + 1))
        raise AutoVerifyError(
            f"{endpoint} failed after {self._max_attempts} attempts: {last_error}"
        )

    def login(self, number: str, password: str, dial_code: str = "91") -> dict:
        """Log in with the account's phone number + password.

        ``numberType`` is the dial code + number, exactly as the site's
        own login form sends it. The dial code may or may not carry a
        leading ``+`` on the server side, so a failed first attempt is
        retried with the ``+`` form before giving up.
        """
        number = str(number).strip()
        candidates: list[str] = []
        if number.startswith("+"):
            candidates.append(number)
        else:
            dial = str(dial_code or "91").strip()
            candidates.append(f"{dial}{number}")
            candidates.append(f"+{dial}{number}")
        seen: set[str] = set()
        candidates = [c for c in candidates if not (c in seen or seen.add(c))]
        last_error: AutoVerifyError | None = None
        for number_type in candidates:
            try:
                data = self._post(
                    "Login",
                    {"numberType": number_type, "pwd": password, "logintype": "mobile"},
                )
            except AutoVerifyError as exc:
                last_error = exc
                continue
            token = data.get("token") or ""
            header = data.get("tokenHeader") or ""
            refresh = data.get("refreshToken") or ""
            if not token or not header:
                last_error = AutoVerifyError("Login response had no session token.")
                continue
            self._token = token
            self._token_header = header
            self._refresh_token = refresh
            logger.info("Auto-verify login succeeded.")
            return {"token": token, "tokenHeader": header, "refreshToken": refresh}
        raise last_error or AutoVerifyError("Login failed.")

    def refresh(self) -> dict:
        """Refresh the session with the refresh token (empty body)."""
        data = self._post("RefreshToken", {}, use_refresh_token=True)
        token = data.get("token") or ""
        if not token:
            raise AutoVerifyError("RefreshToken response had no session token.")
        self._token = token
        if data.get("tokenHeader"):
            self._token_header = data["tokenHeader"]
        if data.get("refreshToken"):
            self._refresh_token = data["refreshToken"]
        logger.info("Auto-verify session refreshed.")
        return {"token": token}

    def get_user_info(self) -> dict:
        """The logged-in account's own info (smoke test + its member UID)."""
        if not self._token:
            raise AutoVerifyError("DmPlay session is not logged in.")
        return self._post("GetUserInfo", {})

    def team_day_report(self, day: str, user_id: int | None, level: int = 0,
                        page_no: int = 1, page_size: int = 10) -> dict:
        """One day of Subordinate data. ``day`` is "YYYY-MM-DD 00:00:00";
        ``user_id`` narrows the list to one subordinate (None = all).
        Returns the data object: {"list": [...rows...], <aggregates>}."""
        if not self._token:
            raise AutoVerifyError("DmPlay session is not logged in.")
        params: dict = {"day": day, "lv": level, "pageNo": page_no, "pageSize": page_size}
        if user_id is not None:
            params["userId"] = int(user_id)
        data = self._post("TeamDayReport", params)
        if "list" not in data:
            raise AutoVerifyError("TeamDayReport payload has no list.")
        return data


def check_uid(client: DmPlayClient, uid: str, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
              min_deposit: float = DEFAULT_MIN_DEPOSIT, today: date | None = None) -> dict:
    """Check one UID against the Promotion data.

    Sums that UID's ``rechargeAmount`` over the last ``lookback_days``
    days (ending yesterday — the site publishes a day late). ``passed``
    is True only when the UID was found in the downline AND the summed
    deposit is over ``min_deposit``. Raises AutoVerifyError on any
    provider failure: the caller must leave the submission pending.
    """
    uid_s = str(uid).strip()
    base = today or datetime.now(timezone.utc).date()
    result: dict = {
        "uid": uid_s,
        "found": False,
        "deposit_total": 0.0,
        "days_checked": [],
        "per_day": [],
        "passed": False,
        "reason": "uid_not_found",
    }
    if not uid_s.isdigit():
        # The site's member UIDs are numbers and its search parses the
        # input as an integer — a non-numeric UID can never match.
        result["reason"] = "uid_not_numeric"
        return result
    target = int(uid_s)
    total = 0.0
    found = False
    for offset in range(1, int(lookback_days) + 1):
        day = base - timedelta(days=offset)
        data = client.team_day_report(_day_string(day), user_id=target)
        result["days_checked"].append(day.isoformat())
        day_total = 0.0
        for row in data.get("list") or []:
            try:
                if int(row.get("userID")) != target:
                    continue
                found = True
                day_total += float(row.get("rechargeAmount") or 0)
            except (TypeError, ValueError):
                continue
        result["per_day"].append({"date": day.isoformat(), "deposit": day_total})
        total += day_total
        if found and total > min_deposit:
            break
    result["found"] = found
    result["deposit_total"] = total
    result["passed"] = found and total > min_deposit
    result["reason"] = (
        "passed" if result["passed"]
        else ("uid_not_found" if not found else "deposit_below_minimum")
    )
    return result


def build_client(settings, client: httpx.Client | None = None) -> DmPlayClient:
    """A DmPlayClient configured from Settings (base URL + origin)."""
    return DmPlayClient(
        base_url=settings.autoverify_base_url,
        origin=settings.autoverify_origin,
        client=client,
    )


def auto_check_summary(result: dict | None) -> str | None:
    """One plain sentence about an auto-check outcome, for admin alerts.

    None when there is nothing to say (not configured, or skipped).
    The caller adds its own "Auto-check: " / "Auto-verify" prefix."""
    if not result:
        return None
    if result.get("skipped"):
        return None
    if result.get("passed"):
        total = result.get("deposit_total", 0.0)
        return f"UID mili, deposit ₹{total:g} confirm hua (₹300 se zyada)."
    reason = result.get("reason")
    if reason == "uid_not_found":
        return "UID Promotion data me nahi mili."
    if reason == "deposit_below_minimum":
        total = result.get("deposit_total", 0.0)
        return f"UID mili, deposit abhi ₹{total:g} hai (₹300 se zyada chahiye) — kal dobara check hoga."
    if reason == "uid_not_numeric":
        return "UID number nahi hai, isliye Promotion data me check nahi ho sakta."
    if reason == "provider_error":
        return "Auto-check abhi nahi ho paya (provider error) — kal dobara check hoga."
    return None


def _record_check(db, row, result: dict) -> None:
    row.auto_check_result = json.dumps(result, ensure_ascii=False)
    row.auto_checked_at = datetime.now(timezone.utc)
    db.commit()


def run_auto_check_for_submission(session_factory, submission_id: int, settings,
                                  client: DmPlayClient | None = None,
                                  today: date | None = None) -> dict:
    """Auto-check one pending submission. Never raises.

    Returns the check result plus ``submission_id`` / ``passed`` /
    ``review_message`` / ``user_telegram_id``. On a clean pass the
    submission is moved to verified via the shared
    ``change_submission_status`` (history + audit, system actor 0);
    anything else leaves it pending_review for the admin queue.
    """
    db = session_factory()
    try:
        row = db.query(Submission).filter(Submission.id == submission_id).first()
        if row is None:
            return {"submission_id": submission_id, "skipped": True, "reason": "not_found"}
        owner_tid = row.user.telegram_id if row.user else None
        if row.status != "pending_review":
            return {"submission_id": submission_id, "skipped": True, "reason": f"status_{row.status}"}
        if not settings.autoverify_configured:
            return {"submission_id": submission_id, "skipped": True, "reason": "not_configured"}
        own_client = client is None
        c = client or build_client(settings)
        try:
            if own_client:
                c.login(
                    settings.autoverify_username,
                    settings.autoverify_password,
                    settings.autoverify_dial_code,
                )
            result = check_uid(
                c,
                row.registration_uid,
                lookback_days=settings.autoverify_lookback_days,
                min_deposit=settings.autoverify_min_deposit,
                today=today,
            )
        finally:
            if own_client:
                c.close()
        result = {**result, "submission_id": submission_id, "user_telegram_id": owner_tid}
        _record_check(db, row, result)
        if not result["passed"]:
            return {**result, "passed": False}
        total = result["deposit_total"]
        user_msg = (
            "Automatic check se verify ho gaya — aapki UID mili aur "
            f"deposit ₹{total:g} confirm hua (₹300 se zyada)."
        )
        note = (
            "Auto-verified by system: UID "
            f"{row.registration_uid} found in Promotion data; deposit ₹{total:g} "
            f"over {len(result['days_checked'])} day(s) checked "
            f"(min ₹{settings.autoverify_min_deposit:g})."
        )
        changed, error, _ = change_submission_status(
            db,
            submission_id=row.id,
            to_status="verified",
            message=user_msg,
            admin_telegram_id=SYSTEM_ADMIN_ID,
            internal_note=note,
        )
        if changed is None:
            logger.warning("Auto-verify status change failed for submission %s: %s", row.id, error)
            return {**result, "passed": False, "reason": "status_change_failed", "error": error}
        return {**result, "passed": True, "review_message": user_msg}
    except AutoVerifyError as exc:
        # Provider problem: fail closed. Record the attempt so the
        # admin can see it, leave the submission pending for the
        # next day's retry / manual review.
        try:
            row = db.query(Submission).filter(Submission.id == submission_id).first()
            if row is not None:
                _record_check(db, row, {"passed": False, "reason": "provider_error",
                                        "error": str(exc), "uid": row.registration_uid})
        except Exception:
            db.rollback()
        logger.warning("Auto-check failed for submission %s: %s", submission_id, exc)
        return {"submission_id": submission_id, "passed": False,
                "reason": "provider_error", "error": str(exc)}
    except Exception:
        logger.exception("Auto-check crashed for submission %s.", submission_id)
        try:
            db.rollback()
        except Exception:
            pass
        return {"submission_id": submission_id, "passed": False, "reason": "internal_error"}
    finally:
        db.close()


def check_pending_submissions(session_factory, settings, client: DmPlayClient | None = None,
                              max_rows: int = 50, min_interval_hours: int = 20,
                              today: date | None = None) -> list[dict]:
    """Re-check pending submissions (daily loop / manual retry).

    Skips rows checked within ``min_interval_hours`` so a restart does
    not re-check the same submissions at once. One login is shared
    across the batch. Returns one result dict per submission actually
    checked (empty when nothing is due or nothing is configured).
    Never raises.
    """
    db = session_factory()
    try:
        rows = (
            db.query(Submission)
            .filter(Submission.status == "pending_review")
            .order_by(Submission.submitted_at)
            .limit(max_rows)
            .all()
        )
        now = datetime.now(timezone.utc)
        due: list[int] = []
        for r in rows:
            checked = r.auto_checked_at
            if checked is not None:
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                if (now - checked).total_seconds() < min_interval_hours * 3600:
                    continue
            due.append(r.id)
        if not due:
            return []
        if not settings.autoverify_configured:
            return [{"submission_id": sid, "skipped": True, "reason": "not_configured"} for sid in due]
        own_client = client is None
        c = client or build_client(settings)
        results: list[dict] = []
        try:
            if own_client:
                c.login(
                    settings.autoverify_username,
                    settings.autoverify_password,
                    settings.autoverify_dial_code,
                )
            for sid in due:
                results.append(run_auto_check_for_submission(
                    session_factory, sid, settings, client=c, today=today))
        except AutoVerifyError as exc:
            logger.warning("Auto-verify batch login failed: %s", exc)
            return [{"submission_id": sid, "passed": False, "reason": "provider_error",
                     "error": str(exc)} for sid in due]
        except Exception:
            logger.exception("Auto-verify batch failed.")
            return results
        finally:
            if own_client:
                c.close()
        return results
    finally:
        db.close()
