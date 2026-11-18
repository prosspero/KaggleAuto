from orchestrator import load_config, check_notebook_status
cfg = load_config()
for i, a in enumerate(cfg['accounts']):
    try:
        status = check_notebook_status(a)
    except Exception as e:
        status = f"ERROR: {e}"
    print(f"Acc#{i+1} ({a['username']}): {status}")
