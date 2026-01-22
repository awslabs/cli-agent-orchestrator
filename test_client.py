#!/usr/bin/env python3
"""CAO API Test Client - Verifies all CRUD operations."""
import requests
import os
import sys

PORT = os.environ.get("CAO_PORT", "8000")
BASE = f"http://localhost:{PORT}/api"

def test(name, passed, detail=""):
    status = "✓" if passed else "✗"
    print(f"  {status} {name}" + (f" - {detail}" if detail else ""))
    return passed

def test_agents():
    print("\n=== AGENTS ===")
    ok = True
    
    # List
    r = requests.get(f"{BASE}/v2/agents")
    ok &= test("List agents", r.status_code == 200, f"{len(r.json())} agents")
    
    # Get
    agents = r.json()
    if agents:
        name = agents[0]["name"]
        r = requests.get(f"{BASE}/v2/agents/{name}")
        ok &= test(f"Get agent '{name}'", r.status_code == 200)
    
    return ok

def test_sessions():
    print("\n=== SESSIONS ===")
    ok = True
    
    # List
    r = requests.get(f"{BASE}/v2/sessions")
    ok &= test("List sessions", r.status_code == 200, f"{len(r.json())} sessions")
    sessions = r.json()
    
    # Get existing session
    if sessions:
        sid = sessions[0]["id"]
        r = requests.get(f"{BASE}/v2/sessions/{sid}")
        ok &= test(f"Get session '{sid[-8:]}'", r.status_code == 200)
        
        # Output
        r = requests.get(f"{BASE}/v2/sessions/{sid}/output")
        ok &= test(f"Get output", r.status_code == 200, f"{len(r.json().get('output', ''))} chars")
        
        # Input
        r = requests.post(f"{BASE}/v2/sessions/{sid}/input?message=test_ping")
        ok &= test(f"Send input", r.status_code == 200)
    
    return ok

def test_beads():
    print("\n=== BEADS ===")
    ok = True
    
    # List
    r = requests.get(f"{BASE}/tasks")
    ok &= test("List beads", r.status_code == 200, f"{len(r.json())} beads")
    
    # Create
    r = requests.post(f"{BASE}/tasks", json={"title": "Test bead", "description": "Testing", "priority": 1})
    ok &= test("Create bead", r.status_code == 201)
    bead_id = r.json()["id"]
    
    # Get
    r = requests.get(f"{BASE}/tasks/{bead_id}")
    ok &= test(f"Get bead '{bead_id}'", r.status_code == 200)
    
    # WIP
    r = requests.post(f"{BASE}/tasks/{bead_id}/wip")
    ok &= test("Mark WIP", r.status_code == 200 and r.json()["status"] == "wip")
    
    # Assign (if sessions exist)
    sessions = requests.get(f"{BASE}/v2/sessions").json()
    if sessions:
        sid = sessions[0]["id"]
        r = requests.post(f"{BASE}/v2/beads/{bead_id}/assign", json={"session_id": sid})
        ok &= test(f"Assign to session", r.status_code == 200 and r.json()["assignee"] == sid)
    
    # Close
    r = requests.post(f"{BASE}/tasks/{bead_id}/close")
    ok &= test("Close bead", r.status_code == 200 and r.json()["status"] == "closed")
    
    # Delete
    r = requests.delete(f"{BASE}/tasks/{bead_id}")
    ok &= test("Delete bead", r.status_code == 200)
    
    # Verify deleted
    r = requests.get(f"{BASE}/tasks/{bead_id}")
    ok &= test("Verify deleted", r.status_code == 404)
    
    return ok

def test_e2e():
    print("\n=== E2E WORKFLOW ===")
    ok = True
    
    # 1. List agents
    r = requests.get(f"{BASE}/v2/agents")
    agents = r.json()
    ok &= test("1. List agents", len(agents) > 0, f"Found {len(agents)}")
    
    # 2. Get sessions
    r = requests.get(f"{BASE}/v2/sessions")
    sessions = r.json()
    ok &= test("2. List sessions", r.status_code == 200, f"Found {len(sessions)}")
    
    if not sessions:
        print("  ! No sessions - skipping session-dependent tests")
        return ok
    
    sid = sessions[0]["id"]
    
    # 3. Create bead
    r = requests.post(f"{BASE}/tasks", json={"title": "E2E Test Task", "priority": 1})
    ok &= test("3. Create bead", r.status_code == 201)
    bead_id = r.json()["id"]
    
    # 4. Assign bead
    r = requests.post(f"{BASE}/v2/beads/{bead_id}/assign", json={"session_id": sid})
    ok &= test("4. Assign bead", r.status_code == 200)
    
    # 5. Send command
    r = requests.post(f"{BASE}/v2/sessions/{sid}/input?message=echo%20E2E_TEST")
    ok &= test("5. Send command", r.status_code == 200)
    
    # 6. Close bead
    r = requests.post(f"{BASE}/tasks/{bead_id}/close")
    ok &= test("6. Close bead", r.status_code == 200)
    
    # 7. Delete bead
    r = requests.delete(f"{BASE}/tasks/{bead_id}")
    ok &= test("7. Delete bead", r.status_code == 200)
    
    return ok

def main():
    print("CAO API Test Client")
    print("=" * 40)
    
    try:
        requests.get(f"{BASE}/health", timeout=2)
    except:
        print("ERROR: Server not running at localhost:8000")
        return 1
    
    results = []
    results.append(("Agents", test_agents()))
    results.append(("Sessions", test_sessions()))
    results.append(("Beads", test_beads()))
    results.append(("E2E", test_e2e()))
    
    print("\n" + "=" * 40)
    print("RESULTS:")
    all_pass = True
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}: {name}")
        all_pass &= passed
    
    print("\n" + ("ALL TESTS PASSED!" if all_pass else "SOME TESTS FAILED"))
    return 0 if all_pass else 1

if __name__ == "__main__":
    sys.exit(main())
