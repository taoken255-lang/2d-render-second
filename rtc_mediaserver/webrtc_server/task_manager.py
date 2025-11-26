import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any
from rtc_mediaserver.config import settings


class TaskManager:
    def __init__(self, status_file: Path):
        self.status_file = status_file
        self.lock = asyncio.Lock()
        self.tasks: Dict[str, Any] = {}
        self.current_task: asyncio.Task = None
        self.current_job_id: str = None

        # при старте загружаем статусы
        if self.status_file.exists():
            try:
                self.tasks = json.loads(self.status_file.read_text())
            except Exception:
                self.tasks = {}

    def set_task(self, task: asyncio.Task, job_id: str):
        self.current_task = task
        self.current_job_id = job_id

        def cb(t: asyncio.Task):
            logging.info(f"Redner task id={self.current_job_id} {t} completed")
            self.current_task = None
            self.current_job_id = None

        self.current_task.add_done_callback(cb)

    async def cancel_task(self, job_id: str):
        if self.current_task and self.current_job_id == job_id:
            logging.info(f"Redner task id={self.current_job_id} canceled")
            self.current_task.cancel()
            self.current_task = None
            self.current_job_id = None
            await self.set_status(job_id, "canceled")

    def is_locked(self):
        return self.current_task is not None


    async def save(self):
        """Асинхронно сохраняет все статусы в файл"""
        async with self.lock:
            tmp_path = self.status_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2))
            tmp_path.replace(self.status_file)

    async def set_status(self, task_id: str, status: str, **extra):
        """Обновляет статус задачи и сохраняет"""
        async with self.lock:
            if task_id not in self.tasks:
                self.tasks[task_id] = {}
            self.tasks[task_id].update({"status": status, **extra})
        await self.save()

    def get(self, task_id: str) -> Dict[str, Any] | None:
        return self.tasks.get(task_id)

    async def delete(self, task_id: str) -> None:
        if task_id in self.tasks:
            del self.tasks[task_id]
            await self.save()


TASK_MANAGER = TaskManager(settings.offline_output_path / "task_status.json")