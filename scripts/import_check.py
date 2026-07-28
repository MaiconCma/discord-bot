import sys
import traceback
from importlib import import_module

sys.path.insert(0, '.')

modules = [
    'cogs.set_system',
    'cogs.bau_system',
    'cogs.vendas_system',
    'cogs.help_system',
    'cogs.economia_system',
    'cogs.anime_reminder_system',
    'cogs.music',
    'cogs.dev_info',
    'cogs.ponto_system',
    'cogs.bolao_copa_system',
    'cogs.solicitacao_arnas',
]

print('Starting import test')
for m in modules:
    try:
        import_module(m)
        print(f'OK: {m}')
    except Exception:
        print(f'ERROR: {m}')
        traceback.print_exc()

print('Done')
