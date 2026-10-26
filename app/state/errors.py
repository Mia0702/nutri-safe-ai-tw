"""持久化狀態的內部例外：區分「不存在 / 已過期」與「儲存後端不可用」。

* 不存在 / 已過期：store 回傳 None（或 claim 回傳 False），不是例外。
* StateUnavailableError：後端連線 / 查詢失敗（呼叫端不得把它當成「過期」）。
* StateSerializationError：資料格式不符（寫入前驗證失敗，或讀到損毀 / 未知版本的紀錄；fail closed）。
訊息不得包含 secret、完整 token 或使用者文字。
"""


class StateStoreError(Exception):
    pass


class StateUnavailableError(StateStoreError):
    pass


class StateSerializationError(StateStoreError):
    pass
