"""分批翻译和一次补译；仅保存当前编辑任务的内存快照，不落盘正文。"""

from collections import deque

from api_client import parse_translation_batch, translation_prompt


def text_batches(blocks, maximum=32, characters=6000):
    batch, length = [], 0
    for block in blocks:
        size = len(block["text"])
        if batch and (len(batch) >= maximum or length + size > characters):
            yield batch
            batch, length = [], 0
        batch.append(block)
        length += size
    if batch:
        yield batch


class TranslationSession:
    def __init__(self, blocks, language):
        self.blocks = {block["id"]: dict(block) for block in blocks}
        self.language = language
        self.queue = deque((batch, False) for batch in text_batches(blocks))
        self.current = []
        self.retry = False
        self.results = {}
        self.failed = set()

    def next_prompt(self):
        if not self.queue:
            return None
        self.current, self.retry = self.queue.popleft()
        return translation_prompt(self.current, self.language, retry=self.retry)

    def accept(self, data):
        ids = [block["id"] for block in self.current]
        originals = {block["id"]: block["text"] for block in self.current}
        valid, pending, _ = parse_translation_batch(data, ids, originals)
        self.results.update(valid)
        if self.retry:
            self.failed.update(pending)
        elif pending:
            # 每个未完成块最多自动补译一次，分成更小的批次避免反复遗漏。
            retries = [(batch, True) for batch in text_batches(
                [self.blocks[block_id] for block_id in pending], maximum=4, characters=1500)]
            self.queue.extendleft(reversed(retries))

    @property
    def progress(self):
        label = "正在补译空译文 / 漏译项" if self.retry else "正在翻译"
        return f"{label}… 已完成 {len(self.results)} / {len(self.blocks)} 块。可取消。"
