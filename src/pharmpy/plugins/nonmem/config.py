from pathlib import Path

from pharmpy.config import ConfigItem, Configuration
from pharmpy.utils import normalize_user_given_path


class NONMEMConfiguration(Configuration):
    module = 'pharmpy.plugins.nonmem'  # TODO: change default
    parameter_names = ConfigItem(
        ['basic'],
        'Naming scheme of NONMEM parameters. Possible settings are "abbr" ($ABBR), "comment", and '
        '"basic". The order denotes priority order',
        list,
    )
    default_nonmem_path = ConfigItem(
        Path(''),
        'Full path to the default NONMEM installation directory',
        cls=normalize_user_given_path,
    )
    write_etas_in_abbr = ConfigItem(False, 'Whether to write etas as $ABBR records', bool)
    licfile = ConfigItem(None, 'Path to the NONMEM license file', cls=normalize_user_given_path)


conf = NONMEMConfiguration()
