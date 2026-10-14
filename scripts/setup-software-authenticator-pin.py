"""Store the virtual Authenticator's existing PIN from a hidden local prompt."""
from pathlib import Path
import getpass
import os

from padelbot.android import AndroidPinStore
from padelbot.config import settings


def main():
    os.chdir(Path(__file__).resolve().parents[1])
    print('KUL Padel - virtuele Authenticator')
    print('Voer de app-PIN in die je bij de registratie van dit virtuele toestel hebt gekozen.')
    print('De invoer blijft verborgen. Er wordt geen login geprobeerd in dit venster.')
    pin = getpass.getpass('App-PIN: ')
    confirmation = getpass.getpass('Herhaal app-PIN: ')
    if pin != confirmation:
        raise ValueError('De twee invoeren verschillen; niets opgeslagen.')
    target = Path('data/software-authenticator/pin.enc')
    AndroidPinStore(settings.padelbot_key_file, target).save(pin)
    print('De virtuele app-PIN is versleuteld opgeslagen.')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nAfgebroken; geen wijziging.')
    except ValueError as error:
        print(str(error))
    except Exception as error:
        print(f'Opslaan mislukt ({type(error).__name__}); geen geheime gegevens getoond.')
    input('Druk Enter om dit venster te sluiten...')
