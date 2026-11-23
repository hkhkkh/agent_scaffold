"""Capability packs: optional ability that travels with an agent.

A pack bundles four things that have to agree: its tools, the Python packages
they import, the permissions its code checks, and the manifest that states all
of it. The platform and an exported agent both load packs through this package,
which is what keeps "works on the canvas" and "works standalone" from becoming
two different capability sets.

Public surface:

* :mod:`app.runtime.packs.manifest` -- the data shape (`PackManifest`);
* :mod:`app.runtime.packs.registry` -- lookup, tool wiring, the doctor report;
* :mod:`app.runtime.packs.builtin` -- the packs shipped with this runtime.

Policy and audit live beside this package (`app.runtime.permissions`,
`app.runtime.audit`) because they outlive any one pack.
"""

from app.runtime.packs.manifest import (
    PACK_API_VERSION,
    ManifestError,
    PackDependency,
    PackManifest,
)
from app.runtime.packs.registry import (
    BUILTIN_PACK_NAMES,
    CapabilityPack,
    PackError,
    PackRegistry,
    builtin_registry,
    doctor_report,
    format_doctor,
    get_pack_registry,
    register_builtin_tools,
    set_pack_registry,
)

__all__ = [
    "BUILTIN_PACK_NAMES",
    "PACK_API_VERSION",
    "CapabilityPack",
    "ManifestError",
    "PackDependency",
    "PackError",
    "PackManifest",
    "PackRegistry",
    "builtin_registry",
    "doctor_report",
    "format_doctor",
    "get_pack_registry",
    "register_builtin_tools",
    "set_pack_registry",
]
