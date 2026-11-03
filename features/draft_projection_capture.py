"""Opt-in capture of ESPN forecasts for stored draft picks, never draft-night grading."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace

import pandas as pd

from features.provider_projections import (
    PROJECTION_COLUMNS, attach_projections, fetch_projections, finite_number,
    load_projection_snapshots, require_projection_schema, save_projection_snapshots,
)


PICK_COLUMNS = ["overall_pick", "team_id", "player_id", "round_num", "round_pick"]


class CaptureExportError(RuntimeError):
    """Storage committed, but the final report directory could not be published."""


class CaptureCommitUncertainError(RuntimeError):
    """A failed commit acknowledgment does not prove the server rolled back."""


def _cleanup(action, label, notices):
    """Cleanup must not hide a committed result or an uncertain commit acknowledgment."""
    try:
        action()
    except Exception as exc:
        notices.append(f"{label} failed ({type(exc).__name__}); primary capture outcome unchanged")


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validated_picks(rows, label):
    result = []
    for row in rows:
        if len(row) != len(PICK_COLUMNS):
            raise ValueError(f"{label} has malformed pick records")
        result.append(tuple(_positive_int(value, f"{label}.{key}") for key, value in zip(PICK_COLUMNS, row)))
    result.sort()
    if not result:
        raise ValueError(f"{label} has no draft picks")
    if len({r[0] for r in result}) != len(result) or len({r[2] for r in result}) != len(result):
        raise ValueError(f"{label} has duplicate pick numbers or players")
    return result


def verify_source_draft(data, stored, league_id, season_year):
    """A season-only DB table must match this exact source league's ordered draft."""
    if not isinstance(data, dict) or type(data.get("id")) is not int or type(data.get("seasonId")) is not int:
        raise ValueError("ESPN response lacks a verified league/season identity")
    if data["id"] != league_id or data["seasonId"] != season_year:
        raise ValueError("ESPN response league/season does not match the requested capture")
    detail = data.get("draftDetail")
    if not isinstance(detail, dict) or not isinstance(detail.get("picks"), list):
        raise ValueError("ESPN response lacks draft picks")
    source = []
    keys = ("overallPickNumber", "teamId", "playerId", "roundId", "roundPickNumber")
    for pick in detail["picks"]:
        if not isinstance(pick, dict):
            raise ValueError("ESPN draft contains malformed picks")
        source.append(tuple(pick.get(key) for key in keys))
    source = _validated_picks(source, "ESPN draft")
    if source != stored:
        raise ValueError("Stored draft picks do not exactly match ESPN league/season/order/team/player/round identity; no forecasts saved")
    status = data.get("status")
    if not isinstance(status, dict):
        raise ValueError("ESPN response lacks season status")
    return _positive_int(status.get("finalScoringPeriod"), "finalScoringPeriod")


def _same_observation(row, fresh):
    if not fresh:
        return False
    left = pd.to_datetime(row.get("projection_observed_at"), errors="coerce", utc=True)
    right = pd.to_datetime(fresh.get("projection_observed_at"), errors="coerce", utc=True)
    return (not pd.isna(left) and not pd.isna(right) and left == right
            and row.get("projection_source") == fresh.get("projection_source")
            and all(finite_number(row.get(key)) == finite_number(fresh.get(key))
                    for key in ("projected_total_points", "projected_avg_points")))


def build_capture_report(picks, stored_projections, fresh, league_id, season_year):
    """Join observations to every pick; never interpret capture time as issue time."""
    report = attach_projections(pd.DataFrame(picks, columns=PICK_COLUMNS), stored_projections)
    report.insert(0, "league_id", league_id)
    report.insert(1, "season_year", season_year)
    report["projection_status"] = [
        "available" if finite_number(row["projected_total_points"]) is not None
        else "average_only" if finite_number(row["projected_avg_points"]) is not None
        else "missing" for row in report.to_dict("records")
    ]
    fresh_by_id = fresh.set_index("player_id").to_dict("index")
    report["observation_selection"] = [
        "unavailable" if row["projection_status"] == "missing"
        else "current_capture" if _same_observation(row, fresh_by_id.get(row["player_id"]))
        else "retained_capture" for row in report.to_dict("records")
    ]
    report["pre_draft_eligibility"] = "not_verified_pre_draft"
    return report.sort_values("overall_pick").reset_index(drop=True)


def capture_draft_projections(conn, request, league_id, season_year, output_dir):
    """Append observations in one transaction; require a new report destination.

    The caller owns connection lifetime. No DDL, draft updates, actuals ingestion,
    grading, email or model calls occur. Existing observations are retained.
    """
    _positive_int(league_id, "league_id")
    _positive_int(season_year, "season_year")
    output_dir = Path(output_dir)
    # lexists: a dangling symlink must be rejected now, not after the commit.
    if os.path.lexists(output_dir):
        raise ValueError("Capture output directory already exists; choose a new directory to preserve prior reports")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    cursor = conn.cursor()
    temporary = None
    committed = False
    commit_started = False
    saved_count = 0
    cleanup_warnings = []
    try:
        try:
            require_projection_schema(cursor)
        except Exception as exc:
            raise ValueError("Projection storage unavailable; review migrations/001_provider_projection_snapshots.sql for the intended database. No automatic migration is performed.") from exc
        cursor.execute("""SELECT overall_pick, team_id, player_id, round_num, round_pick
                          FROM draft_picks WHERE season_year = %s ORDER BY overall_pick""", (season_year,))
        picks = _validated_picks(cursor.fetchall(), "Stored draft")
        try:
            data = request.league_get(params={"view": ["mSettings", "mDraftDetail"]})
        except Exception as exc:
            # Library messages can embed request headers, including ESPN cookies.
            raise ValueError(f"ESPN draft request failed ({type(exc).__name__}); "
                             "request details suppressed. No forecasts saved.") from None
        period = verify_source_draft(data, picks, league_id, season_year)
        league = SimpleNamespace(year=season_year, finalScoringPeriod=period, espn_request=request)
        fresh = fetch_projections(league, [pick[2] for pick in picks])
        saved_count = sum(finite_number(row.get("projected_total_points")) is not None
                          or finite_number(row.get("projected_avg_points")) is not None
                          for row in fresh.to_dict("records"))
        save_projection_snapshots(cursor, fresh, league_id)
        retained = load_projection_snapshots(conn, league_id, season_year)
        report = build_capture_report(picks, retained, fresh, league_id, season_year)
        summary = {
            "season_year": season_year, "year_convention": "NBA season ending year",
            "source_draft_identity": "exact ordered match to stored picks including rounds",
            "draft_pick_count": len(picks), "saved_observations": saved_count,
            "fresh_total_forecasts": int(fresh["projected_total_points"].notna().sum()),
            "fresh_average_forecasts": int(fresh["projected_avg_points"].notna().sum()),
            "reported_total_forecasts": int(report["projected_total_points"].notna().sum()),
            "retained_observations": int((report["observation_selection"] == "retained_capture").sum()),
            "missing_forecasts": int((report["projection_status"] == "missing").sum()),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "database_commit": "completed",
            "storage": "provider_projection_snapshots (existing migration 001; append only)",
            "pre_draft_eligibility": "not_verified_pre_draft",
            "limitation": "Capture timestamps record this observation, not when ESPN issued the forecast. No draft-night grade is produced.",
            "report_directory": str(output_dir),
            "cleanup_warnings": cleanup_warnings,
        }
        temporary = Path(tempfile.mkdtemp(prefix=".draft-capture-", dir=output_dir.parent))
        report.to_csv(temporary / "draft_pick_projections.csv", index=False)
        (temporary / "capture_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        commit_started = True
        conn.commit()
        committed = True
        if os.path.lexists(output_dir):
            raise FileExistsError("Destination appeared during capture")
        temporary.rename(output_dir)
        temporary = None
        return summary
    except Exception as exc:
        if committed:
            # Keep the prepared report: it is the only export of committed rows.
            preserved, temporary = temporary, None
            location = f" Prepared report kept at {preserved}." if preserved is not None else ""
            raise CaptureExportError(
                f"Database capture committed {saved_count} observations, but report export failed.{location} "
                "Do not assume rollback or automatically repeat the capture."
            ) from exc
        if commit_started:
            try:
                conn.rollback()
            except Exception:
                pass
            raise CaptureCommitUncertainError(
                "Database commit acknowledgment failed; capture outcome is uncertain. "
                "Inspect stored observations before retrying."
            ) from exc
        _cleanup(conn.rollback, "Rollback cleanup", cleanup_warnings)
        raise
    finally:
        _cleanup(cursor.close, "Cursor cleanup", cleanup_warnings)
        # After commit the prepared report is the only export; never delete it
        # (covers interrupts, which bypass the except block above).
        if temporary is not None and not committed:
            _cleanup(lambda: shutil.rmtree(temporary), "Temporary report cleanup", cleanup_warnings)


def run_from_environment(season_year, output_dir):
    """Live boundary, invoked only by the explicit draft capture CLI option."""
    from database import get_connection
    from espn_api.requests.espn_requests import EspnFantasyRequests
    try:
        league_id = int(os.environ["ESPN_LEAGUE_ID"])
    except (KeyError, ValueError):
        raise ValueError("ESPN_LEAGUE_ID must be set to an integer league ID") from None
    request = EspnFantasyRequests(sport="nba", year=season_year, league_id=league_id,
                                 cookies={"espn_s2": os.environ["ESPN_S2"], "SWID": os.environ["ESPN_SWID"]})
    conn = get_connection()
    result = None
    cleanup_warnings = []
    try:
        result = capture_draft_projections(conn, request, league_id, season_year, output_dir)
        return result
    finally:
        _cleanup(conn.close, "Connection cleanup", cleanup_warnings)
        if result is not None:
            result.setdefault("cleanup_warnings", []).extend(cleanup_warnings)
