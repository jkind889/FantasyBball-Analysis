-- REVIEW AND APPLY EXPLICITLY to the intended MySQL database before a weekly run.
-- Additive only: no backfill from actuals, legacy zeros, or current estimates.
CREATE TABLE provider_projection_snapshots (
    snapshot_id CHAR(36) NOT NULL PRIMARY KEY,
    league_id BIGINT NOT NULL,
    player_id BIGINT NOT NULL,
    season_year INT NOT NULL,
    horizon VARCHAR(32) NOT NULL,
    source VARCHAR(64) NOT NULL,
    observed_at DATETIME(6) NOT NULL COMMENT 'UTC capture time; not provider issue time',
    projected_total_points DOUBLE NULL,
    projected_avg_points DOUBLE NULL,
    INDEX projection_lookup (league_id, season_year, player_id, horizon, observed_at)
);
