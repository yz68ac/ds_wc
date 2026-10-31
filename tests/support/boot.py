"""Load the recovery entry without executing its main function."""
import importlib.util
from tests.support.paths import ROOT

spec = importlib.util.spec_from_file_location('boot_recovery',ROOT/'scripts/boot-recovery.py')
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)
