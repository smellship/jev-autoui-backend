"""测试会话的硬隔离：用例永不读到真实密钥，任何输入都不会联网调用付费模型。

真实环境变量的优先级高于 .env，所以把两个密钥钉成空串即可覆盖所有 Settings() 构造；
显式传参（如 Settings(text_model_api_key="k")）的用例不受影响，它们必须自己打桩运输层。
"""

from __future__ import annotations

import os

os.environ["TEXT_MODEL_API_KEY"] = ""
os.environ["TYPESAFE_API_KEY"] = ""
