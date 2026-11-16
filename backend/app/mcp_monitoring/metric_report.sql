, samples AS (
    SELECT f.tool, m.key, d.descriptor, f.duration_ms, m.value,
           $9::jsonb -> (f.tool || '/' || m.key) AS view
    FROM filtered f CROSS JOIN LATERAL jsonb_each(f.samples) m
    JOIN mcp_monitoring.metric_definitions d ON (d.tool,d.key)=(f.tool,m.key)
    WHERE f.outcome='ok'
), grouped AS (
    SELECT tool, key, descriptor, view,
        CASE WHEN descriptor->>'kind'='latency' THEN
            CASE WHEN value ? (view->>'group')
                 THEN jsonb_build_object(view->>'group', value->(view->>'group'))
                 ELSE value END
        END AS dimensions,
        count(*) AS calls, count(*) FILTER (WHERE value='true'::jsonb) AS matched,
        percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms)
            FILTER (WHERE descriptor->>'kind'='latency') AS p95_ms
    FROM samples GROUP BY 1,2,3,4,5
), ranked AS (
    SELECT *, row_number() OVER w AS rn, count(*) OVER (PARTITION BY tool,key) AS total,
           sum(calls) OVER (PARTITION BY tool,key)::bigint AS measured,
           coalesce((view->>'page')::int,0)*$10::int AS skip
    FROM grouped WINDOW w AS (PARTITION BY tool,key ORDER BY calls DESC, dimensions)
)
SELECT *, rn BETWEEN skip+1 AND skip+$10 AS visible FROM ranked
WHERE rn=1 OR rn BETWEEN skip+1 AND skip+$10 ORDER BY tool,key,rn
