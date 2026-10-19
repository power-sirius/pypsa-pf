"""A tiny in-memory stand-in for the PowerFactory Python API.

It records the object tree created by ``pypsa_pf.powerfactory_builder`` so the
builder can be tested without PowerFactory. It does NOT validate attribute
names or compute load flows (results "m:..." return ``mock_results`` or 1.0).
"""

from __future__ import annotations

import fnmatch


class PFObject:
    def __init__(self, cls: str, name: str = "", parent: "PFObject | None" = None):
        object.__setattr__(self, "_cls", cls)
        object.__setattr__(self, "_attrs", {"loc_name": name})
        object.__setattr__(self, "_children", [])
        object.__setattr__(self, "_parent", parent)

    # attribute access like the real API
    def __getattr__(self, item):
        attrs = object.__getattribute__(self, "_attrs")
        if item in attrs:
            return attrs[item]
        raise AttributeError(item)

    def __setattr__(self, key, value):
        self._attrs[key] = value

    def __repr__(self):
        return f"<{self.loc_name}.{self._cls}>"

    # API methods used by the builder
    def GetClassName(self):
        return self._cls

    def CreateObject(self, cls: str, name: str = ""):
        child = PFObject(cls, name, self)
        self._children.append(child)
        return child

    def GetContents(self, pattern: str = "*", recursive: int = 0):
        out = []
        for c in self._children:
            if fnmatch.fnmatch(f"{c.loc_name}.{c._cls}", pattern) or (pattern == "*"):
                out.append(c)
            if recursive:
                out.extend(c.GetContents(pattern, recursive))
        return out

    def AddCopy(self, other: "PFObject", name: str = ""):
        def clone(src, parent, new_name=None):
            obj = parent.CreateObject(src._cls, new_name or src.loc_name)
            obj._attrs.update({k: v for k, v in src._attrs.items() if k != "loc_name"})
            for child in src._children:
                clone(child, obj)
            return obj
        return clone(other, self, name or other.loc_name)

    def SlotUpdate(self):
        slots = list(self._attrs.get("pelm") or [])
        for child in self._children:
            if child._cls == "ElmDsl" and child.loc_name in ("gov", "avr"):
                slots[{"gov": 1, "avr": 3}[child.loc_name]] = child
        self._attrs["pelm"] = slots
        return 0

    def GetFullName(self):
        return (self._parent.GetFullName() + "\\" if self._parent else "") + f"{self.loc_name}.{self._cls}"

    def Delete(self):
        self._parent._children.remove(self)
        return 0

    def Activate(self):
        return 0

    def Deactivate(self):
        return 0

    def CopyData(self, other: "PFObject"):
        self._attrs.update({k: v for k, v in other._attrs.items() if k != "loc_name"})

    def GetAttribute(self, name: str):
        if name.startswith("m:"):
            return self._attrs.get(name, 1.0)
        return self._attrs.get(name)

    def SetAttribute(self, name: str, value):
        self._attrs[name] = value

    def Execute(self):
        return 0

    def walk(self):
        for c in self._children:
            yield c
            yield from c.walk()


class Application:
    def __init__(self, templates: dict[str, dict] | None = None):
        self.project = PFObject("IntPrj", "Test project")
        self.netdat = self.project.CreateObject("IntPrjfolder", "Network Data")
        self.equip = self.project.CreateObject("IntPrjfolder", "Equipment Type Library")
        self.study = self.project.CreateObject("IntPrjfolder", "Study Cases")
        self.case = self.study.CreateObject("IntCase", "Study Case")
        self.ldf = self.case.CreateObject("ComLdf", "Load Flow")
        self.templates_grid = self.netdat.CreateObject("ElmNet", "Templates")
        plant = self.templates_grid.CreateObject("ElmComp", "SG plant")
        plant.pelm = [None] * 15
        gov = plant.CreateObject("ElmDsl", "gov")
        gov.typ_id = PFObject("BlkDef", "gov_IEEE_TGOV1")
        gov.params = [0.0, 0.05, 0.5, 1.0, 3.0, 0.0, 0.0, 1.0]
        plant.CreateObject("ElmDsl", "avr")
        vsm = self.templates_grid.CreateObject("ElmComp", "VSM")
        vsm_control = vsm.CreateObject("ElmDsl", "vsm_control")
        vsm_control.typ_id = PFObject("BlkDef", "gfc_virtual synchronous machine")
        vsm_control.params = [3.0, 20.0, 0.0, 0.003, 1.0]
        vsm.pelm = [None,
                    vsm_control,
                    vsm.CreateObject("ElmDsl", "vi"),
                    vsm.CreateObject("ElmDsl", "vco"),
                    vsm.CreateObject("ElmDsl", "pq"),
                    vsm.CreateObject("ElmDsl", "ovc"),
                    vsm.CreateObject("StaVmea", "Voltage Measurement"),
                    vsm.CreateObject("StaImea", "Current Measurement")]
        hv = self.templates_grid.CreateObject("ElmComp", "hvdc")
        hv_dsl = hv.CreateObject("ElmDsl", "VSC HVDC Model")
        hv_dsl.params = [float(i) for i in range(59)]
        hv_dsl.params[19], hv_dsl.params[26] = 1.05, 500.0  # Pmax, Udref
        hv.pelm = [None, None, hv_dsl, hv.CreateObject("StaVmea", "Voltage Measurement"),
                   hv.CreateObject("StaVmea", "Voltage Measurement(1)")]
        hf = self.templates_grid.AddCopy(hv, "hvdc f-ctrl")
        hf.pelm = [None, None] + [c for c in hf._children] + [hf.CreateObject("StaPll", "PLL"),
                                                              hf.CreateObject("ElmDsl", "ctrl")]
        hf._children[-1].params = [1.0, 0.0, 0.0, 0.0]
        self.events = self.case.CreateObject("IntEvt", "Simulation Events/Fault")
        self.events.CreateObject("EvtLod", "Load Event")
        self.results = self.case.CreateObject("ElmRes", "All calculations")
        self.results.CreateObject("IntMon", "old gen.ElmSym")
        for name, attrs in (templates or {}).items():
            t = self.equip.CreateObject("TypSym", name)
            for k, v in attrs.items():
                setattr(t, k, v)

    def GetActiveProject(self):
        return self.project

    def GetActiveStudyCase(self):
        return self.case

    def ActivateProject(self, name):
        return 0

    def GetProjectFolder(self, kind):
        return {"netdat": self.netdat, "equip": self.equip, "study": self.study}[kind]

    def GetFromStudyCase(self, cls):
        return self.ldf

    def Show(self):
        pass

    def EchoOff(self):
        pass

    def EchoOn(self):
        pass

    def ClearOutputWindow(self):
        pass

    def Rebuild(self, mode):
        pass

    def GetDataManager(self):
        return None


def GetApplication():  # noqa: N802  (mirrors the real API)
    return Application()
