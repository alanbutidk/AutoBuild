import sys
# Custom classes
class NotRegistered:
    pass
class Registered:
    pass
class NoKwargs:
    pass
class OverlimitKwargs:
    pass
# End of Custom Classes
# Start of ArgHandle
class ArgHandle:
    def __init__(self):
        self.args = sys.argv[1:]
        self._ProgramName = "PROGRAM"
        self._ArgsRegistered = {
            "help": {
                "Flags": ["--help", "-h"],
                "HelpMsg": "Prints this help message and exit"
            }
        }

    @staticmethod
    def ArgCount() -> None | int:
        return len(sys.argv)
    @staticmethod
    def PrintOnNoArgs(string: str, Exit=False):
        if len(sys.argv) < 2:
            if Exit:
                raise SystemExit(f"{string}\n")
            print(f"{string}\n")
    def IsArgMatch(self, String: str, AtIndex: int) -> tuple | bool:
        if String == self.args[AtIndex]:
            return True
        else:
            return False
    def IsArgInActualArgs(self, String: str) -> str | bool:
        if String in self.args:
            return True
        else:
            return False
    def ProgramName(self, string: str):
        self._ProgramName = string
    def RegisterToHelp(self, Argument: str, RepresentingCommands: list, **kwargs) -> NoKwargs | NotRegistered | Registered | OverlimitKwargs | tuple:
        """This function registers a cmd to --help, -h
        By Default, help is registered to you.
        How to use it:
        RegisterToHelp("Version", ["--version"])
        kwargs: HelpMsg=\"This is the help msg that will print with it\"
        Format to print:
        [--help, -h]: Prints this help message and exit
        """
        if Argument == "":
            return NotRegistered()
        if not kwargs:
            return NoKwargs()
        if len(kwargs) > 1:
            return OverlimitKwargs()
        if RepresentingCommands == []:
            return NotRegistered()
        Cmd, Msg = next(iter(kwargs.items()))
        self._ArgsRegistered[Argument] = {
            "Flags": RepresentingCommands,
            "HelpMsg": Msg
        }
        return Registered()

    def HandleHelp(self, Exit=True):
        if not any(arg in self.args for arg in ["--help", "-h"]):
            return
        print(f"[{self._ProgramName}] --help called:")
        for Name, Info in self._ArgsRegistered.items():
            Flags = ", ".join(Info["Flags"])
            Msg = Info["HelpMsg"]
            print(f"  [{Flags}]: {Msg}")
        if Exit:
            raise SystemExit()
