"""The descriptions the framework writes itself, for the built-in tools (requirement 3.9).

Every description of a pack tool comes from that pack's ``tools/descriptions/`` directory
and goes into tools/list verbatim (requirement 3.7). The one tool that belongs to no pack,
``business_context_get`` (:data:`~erp_mock.tools.generator.BUSINESS_CONTEXT_GET`), has no
such file: it is part of every instance, so its text is part of the framework and does not
change with the business packs or the instance configuration (requirement 3.9). That text
is :data:`BUILTIN_DESCRIPTIONS`, read by :func:`~erp_mock.tools.descriptions.build_profile_tools`
the way a pack's text is read from its description file.

The text is written for the zh-detailed variant, the one variant M0 serves (requirement 2.13
pins the profile to it), in the five parts of tool-design.md section 3 - what the tool does,
where it applies, its preconditions, its side effects and what it returns - and it stays
within the 400 code points requirement 3.3 allows a tool description. The tool takes no
parameter, so it has no parameter description.
"""

from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.pack import ToolDescription
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET

__all__ = ["BUILTIN_DESCRIPTIONS"]

# The zh-detailed text of business_context_get. It names what the result carries
# (requirement 16.10) and nothing of any business pack, so it reads the same whatever
# configuration is loaded.
_BUSINESS_CONTEXT_GET: Final = """\
获取当前会话的业务上下文：业务日期、会计期间、缺省组织和当前用户。
新建单据时不填业务日期和组织字段，服务按这里返回的值填充；如需查看一条基础资料或一张单据，请使用对应的 get 工具。
前置条件：无，本工具不接受任何参数。
副作用：无，只读，不改变沙箱中的数据。
返回：business_date（业务日期，YYYY-MM-DD）、current_period（当前会计期间，YYYY-MM）、org（缺省组织，含 id、code、name）、user（当前用户，含 id、code、name）。"""

# The descriptions of the built-in tools, by tool name: the text of the zh-detailed
# variant, which is the only variant M0 serves.
BUILTIN_DESCRIPTIONS: Final[FrozenMap[str, ToolDescription]] = FrozenMap(
    {BUSINESS_CONTEXT_GET: ToolDescription(description=_BUSINESS_CONTEXT_GET)}
)
