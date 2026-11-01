import os
import json
import time
import threading
import psutil
import valkey
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from pymongo import MongoClient

# Automatically load the .env file from the current directory
load_dotenv()

from langgraph.checkpoint.mongodb import MongoDBSaver
from app.database import MongoDBClient
from app.executor import WorkflowExecutionController

# --- Configuration from .env ---
VALKEY_HOST = os.getenv("VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.getenv("VALKEY_PORT", 6379))
STREAM_NAME = os.getenv("VALKEY_STREAM_NAME", "agentic_tasks")
GROUP_NAME = os.getenv("VALKEY_GROUP_NAME", "agentic_workers")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", "worker_node_1")
MAX_RAM_PERCENT = float(os.getenv("MAX_RAM_PERCENT", 90.0))
CLAIM_TIMEOUT_MS = int(os.getenv("CLAIM_TIMEOUT_MS", 600000))

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB_NAME", "aiqod-dev")

# --- Global Clients ---
valkey_client = valkey.Valkey(host=VALKEY_HOST, port=VALKEY_PORT, decode_responses=True)
db_client = MongoDBClient(uri=MONGO_URI, db_name=DB_NAME)

def setup_valkey_group():
    try:
        valkey_client.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
        print(f"[System] Created Valkey Consumer Group: {GROUP_NAME}")
    except valkey.exceptions.ResponseError as e: 
        if "BUSYGROUP" not in str(e):
            raise

def process_task(executor, message_id, message_data):
    print(f"[{CONSUMER_NAME}] Processing Task {message_id} on thread: {threading.current_thread().name}")
    execution_id = message_data.get("execution_id") 
    
    try:
        action = message_data.get("action")

        if action == "start":
            workflow_id = message_data.get("workflow_id")
            initial_state = json.loads(message_data.get("initial_state", "{}"))
            db_client.update_status(execution_id, "running")
            executor.start_workflow(execution_id, workflow_id, initial_state)

        elif action == "resume":
            payload = json.loads(message_data.get("payload", "{}"))
            userid = message_data.get("userid")
            roleid = message_data.get("roleid")
            executor.resume_workflow(execution_id, payload, userid, roleid)

        valkey_client.xack(STREAM_NAME, GROUP_NAME, message_id)
        print(f"[{CONSUMER_NAME}] Task {message_id} completed and acknowledged.")
        
    except Exception as e:
        print(f"[{CONSUMER_NAME}] Error processing message {message_id}: {e}")
        if execution_id:
            try:
                db_client.update_status(execution_id, "failed")
                valkey_client.publish(
                    f"execution_stream:{execution_id}",
                    json.dumps({"type": "failed", "data": {"error": f"Fatal Worker Error: {str(e)}"}})
                )
            except Exception as db_err:
                pass
        
        # Poison Pill Defense: Discard task so it doesn't loop forever
        try:
            valkey_client.xack(STREAM_NAME, GROUP_NAME, message_id)
            print(f"[{CONSUMER_NAME}] Poison pill task {message_id} safely discarded.")
        except Exception:
            pass

def recover_stale_instances():
    stale = db_client.find_stale_instances(stale_after_minutes=15)
    if not stale:
        return
    print(f"[Recovery] Found {len(stale)} stale instance(s). Re-enqueuing...")
    for inst in stale:
        try:
            payload = {
                "action": "start",
                "execution_id": inst.execution_id,
                "workflow_id": inst.workflow_id,
                "initial_state": json.dumps(inst.initial_inputs),
            }
            valkey_client.xadd(STREAM_NAME, payload)
            print(f"[Recovery] Re-enqueued stale instance {inst.execution_id}")
        except Exception as e:
            print(f"[Recovery] Failed to re-enqueue {inst.execution_id}: {e}")

# ---------------------------------------------------------------------------
# Build jobs (additive) — separate stream/group/pool; see services/build_consumer.py
# Runtime code above is unchanged from the pre-build worker.
# ---------------------------------------------------------------------------
from services import build_consumer as _build

def worker_loop():
    print(f"[Worker] Starting RAM-aware consumer: {CONSUMER_NAME}")
    
    mongo_client = MongoClient(MONGO_URI)
    checkpointer = MongoDBSaver(mongo_client, db_name=DB_NAME)
    executor = WorkflowExecutionController(db_client, checkpointer, valkey_client)

    setup_valkey_group()
    recover_stale_instances()

    max_threads = os.cpu_count() or 4
    thread_pool = ThreadPoolExecutor(max_workers=max_threads)
    print(f"[Worker] Thread pool initialized with {max_threads} concurrent workers.")

    # --- Build (additive only) ---
    _build.setup_build_valkey_group(valkey_client)
    build_pool = _build.create_build_pool()
    _build.log_build_ready()
    mongo_db = db_client.db
    _build.recover_stale_build_jobs(mongo_db, valkey_client)

    while True:
        current_ram = psutil.virtual_memory().percent
        if current_ram > MAX_RAM_PERCENT:
            print(f"[Worker] High RAM ({current_ram}%). Pausing...")
            time.sleep(5)
            continue

        try:
            pending_messages = valkey_client.xpending_range(
                name=STREAM_NAME, 
                groupname=GROUP_NAME, 
                min="-", 
                max="+", 
                count=10
            )
            
            message_ids_to_claim = [
                msg['message_id'] for msg in pending_messages 
                if msg['time_since_delivered'] >= CLAIM_TIMEOUT_MS
            ]
            
            if message_ids_to_claim:
                claimed_messages = valkey_client.xclaim(
                    name=STREAM_NAME,
                    groupname=GROUP_NAME,
                    consumername=CONSUMER_NAME,
                    min_idle_time=CLAIM_TIMEOUT_MS,
                    message_ids=message_ids_to_claim
                )
                if claimed_messages:
                    print(f"[{CONSUMER_NAME}] Recovered {len(claimed_messages)} abandoned tasks.")
                    for message_id, message_data in claimed_messages:
                        thread_pool.submit(process_task, executor, message_id, message_data)
                    _build.poll_build_once(
                        valkey_client,
                        mongo_db,
                        build_pool,
                        CONSUMER_NAME,
                    )
                    continue

            messages = valkey_client.xreadgroup(
                groupname=GROUP_NAME,
                consumername=CONSUMER_NAME,
                streams={STREAM_NAME: ">"},
                count=1,
                block=2000 
            )

            if messages:
                for stream, message_list in messages:
                    for message_id, message_data in message_list:
                        thread_pool.submit(process_task, executor, message_id, message_data)

            # Build poll every tick (non-blocking). Uses BUILD_CLAIM_TIMEOUT_MS
            # internally — not the runtime CLAIM_TIMEOUT_MS (10m).
            _build.poll_build_once(
                valkey_client,
                mongo_db,
                build_pool,
                CONSUMER_NAME,
            )

        except Exception as e:
            print(f"[{CONSUMER_NAME}] Error in worker loop: {e}")
            time.sleep(2)

if __name__ == "__main__":
    worker_loop()
