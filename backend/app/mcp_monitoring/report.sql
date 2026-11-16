, bucketed AS (
    SELECT *, date_bin($9::interval, finished_at, $1::timestamptz) AS bucket FROM filtered
), grouped AS (
    SELECT CASE WHEN grouping(bucket)=0 THEN 'series' WHEN grouping(tool)=0 THEN 'tools'
                WHEN grouping(auth_kind)=0 THEN 'credentials' WHEN grouping(actor_id)=0 THEN 'users'
                ELSE 'summary' END AS section,
           bucket, actor_id, auth_kind, CASE WHEN $3::uuid IS NOT NULL THEN pat_id END AS pat_id, tool,
           count(*) AS calls, count(DISTINCT actor_id) AS active_users, max(finished_at) AS last_observed_at,
           count(*) FILTER (WHERE auth_kind='oauth') AS sso_calls,
           count(*) FILTER (WHERE auth_kind='pat') AS pat_calls,
           count(*) FILTER (WHERE auth_kind='service') AS service_key_calls,
           count(*) FILTER (WHERE outcome IN ('tool_error','rpc_error')) AS errors,
           count(*) FILTER (WHERE outcome IN ('ok','tool_error','rpc_error')) AS decided,
           percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
    FROM bucketed
    GROUP BY GROUPING SETS ((), (bucket), (actor_id), (tool), (auth_kind, CASE WHEN $3::uuid IS NOT NULL THEN pat_id END))
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY section ORDER BY
               CASE WHEN section='tools' AND $14::text='errors' THEN errors END DESC NULLS LAST,
               CASE WHEN section='tools' AND $14::text='latency' THEN p95_ms END DESC NULLS LAST,
               calls DESC, actor_id, auth_kind, pat_id, tool) AS rn,
           count(*) OVER (PARTITION BY section) AS group_count,
           CASE section WHEN 'users' THEN $11::int WHEN 'credentials' THEN $12::int
                        WHEN 'tools' THEN $13::int ELSE 0 END AS skip
    FROM grouped
)
SELECT r.*, t.status AS credential_status, t.expires_at AS pat_expires_at, group_count > skip + $10::int AS has_more,
       CASE section WHEN 'users' THEN coalesce(nullif(u.display_name,''),u.username)
                    WHEN 'credentials' THEN t.name END AS label
FROM ranked r
LEFT JOIN users u ON r.section='users' AND u.id=r.actor_id
LEFT JOIN pat_status t ON r.section='credentials' AND t.id=r.pat_id AND t.user_id=$3
WHERE section IN ('summary','series') OR rn BETWEEN skip+1 AND skip+$10
ORDER BY section,rn
