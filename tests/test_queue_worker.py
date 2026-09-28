from seriesai.db import Database
from seriesai.services.queue_worker import QueueWorkerService
import json


def test_worker_claim_complete_and_resume(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json) VALUES('job','demo','draft_shot_queue','pending','{}')")
        connection.execute("INSERT INTO generation_job_items(item_id,job_id,item_type,sequence_number,status,checkpoint_json) VALUES('item','job','shot',1,'pending','{}')")
    worker = QueueWorkerService(database)
    claim = worker.claim_next('job', 'worker-a')
    assert claim and claim['item_id'] == 'item'
    assert worker.claim_next('job', 'worker-b') is None
    assert worker.complete('item', 'worker-a', 'Series/Generated/test.mp4')['status'] == 'completed'


def test_worker_recovers_interrupted_claim(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json) VALUES('job','demo','draft_shot_queue','pending','{}')")
        connection.execute("INSERT INTO generation_job_items(item_id,job_id,item_type,sequence_number,status,checkpoint_json) VALUES('item','job','shot',1,'pending','{}')")
    worker = QueueWorkerService(database)
    worker.claim_next('job', 'interrupted-worker')
    assert worker.recover_interrupted()['recovered_items'] == 1
    assert worker.claim_next('job', 'replacement-worker')['worker_id'] == 'replacement-worker'


def test_worker_claims_next_pending_shot_across_jobs(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        for job, sequence in (("job-a", 2), ("job-b", 1)):
            connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json) VALUES(?, 'demo','draft_shot_queue','pending','{}')", (job,))
            connection.execute("INSERT INTO generation_job_items(item_id,job_id,item_type,sequence_number,status,checkpoint_json) VALUES(?,?,'shot',?,'pending','{}')", (job + "-item", job, sequence))
    claim = QueueWorkerService(database).claim_next_any("remote")
    assert claim and claim["worker_id"] == "remote"
    assert claim["item_id"] == "job-a-item"


def test_worker_cannot_claim_second_item_while_first_renders(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json) VALUES('job','demo','draft_shot_queue','pending','{}')")
        for number in (1, 2):
            connection.execute("INSERT INTO generation_job_items(item_id,job_id,item_type,sequence_number,status,checkpoint_json) VALUES(?, 'job','shot',?,'pending','{}')", (f"item-{number}", number))
    worker = QueueWorkerService(database)
    assert worker.claim_next_any("gpu-worker")["item_id"] == "item-1"
    assert worker.claim_next_any("gpu-worker") is None
    assert worker.claim_next_any("other-worker")["item_id"] == "item-2"


def test_worker_persists_backend_submission_before_completion(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json) VALUES('job','demo','draft_shot_queue','pending','{}')")
        connection.execute("INSERT INTO generation_job_items(item_id,job_id,item_type,sequence_number,status,checkpoint_json) VALUES('item','job','shot',1,'pending','{}')")
    worker = QueueWorkerService(database); worker.claim_next_any("gpu")
    worker.record_backend_submission("item", "gpu", "comfyui", "prompt-123")
    with database.connect() as connection:
        spec = json.loads(connection.execute("SELECT checkpoint_json FROM generation_job_items WHERE item_id='item'").fetchone()[0])
    assert spec["backend_submission"]["prompt_id"] == "prompt-123"


def test_worker_adopts_crash_surviving_local_output(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    source = tmp_path / "comfy.mp4"; source.write_bytes(b"video")
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json) VALUES('job','demo','draft_shot_queue','pending','{}')")
        connection.execute("INSERT INTO generation_job_items(item_id,job_id,item_type,sequence_number,status,checkpoint_json) VALUES('item','job','shot',1,'pending',?)", (json.dumps({"output_path": "Series/out.mp4"}),))
    worker = QueueWorkerService(database); worker.claim_next_any("gpu")
    result = worker.adopt_local_output("item", "gpu", str(source), str(tmp_path))
    assert result["status"] == "completed"
    assert (tmp_path / "Series" / "out.mp4").read_bytes() == b"video"
