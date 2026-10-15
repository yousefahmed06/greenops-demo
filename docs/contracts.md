{ "name": "nightly-backup", "image": "demo-job:1",
"duration_hours": 2, "deadline": "2026-10-14T08:00:00Z",
"power_kw": 0.1, "needs_gpu": false }

{ "id": 1, "status": "Scheduled",
"region": "sweden", "region_key": "swedencentral",
"provider": "Microsoft Azure",
"start": "2026-10-14T02:00:00Z",
"carbon_g": 12.4, "naive_region": "germany",
"naive_carbon_g": 31.0, "saved_pct": 60.0,
"skipped": [], "reason": "Lowest average carbon before deadline" }
