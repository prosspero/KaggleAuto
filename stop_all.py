import sys
from ghactions_orchestrator import get_accounts, stop_notebook, check_status, load_state, save_state, record_event

def main():
    accounts = get_accounts()
    state = load_state()
    print(f"Checking and stopping all sessions across {len(accounts)} accounts...")
    
    stopped_any = False
    for i, a in enumerate(accounts):
        username = a['username']
        st = check_status(a)
        print(f"Account #{i+1} ({username}): {st}")
        if st in ('running', 'queued', 'cancel_requested', 'cancelacknowledged'):
            print(f"-> Stopping account #{i+1} ({username})...")
            ok = stop_notebook(a, wait_for_stop=True, max_wait_sec=300)
            print(f"-> Stop result for {username}: {ok}")
            record_event(state, "manual_stop", account=username, reason="user_requested_stop_all")
            stopped_any = True
            
    state["current_session_start"] = None
    save_state(state)
    print("Done. All accounts checked/stopped.")

if __name__ == "__main__":
    main()
