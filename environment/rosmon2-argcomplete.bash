if type register-python-argcomplete3 > /dev/null 2>&1; then
  eval "$(register-python-argcomplete3 mon2)"
  eval "$(register-python-argcomplete3 rosmon2)"
elif type register-python-argcomplete > /dev/null 2>&1; then
  eval "$(register-python-argcomplete mon2)"
  eval "$(register-python-argcomplete rosmon2)"
fi
