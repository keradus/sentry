from sentry.integrations.msteams.constants import SALT
from sentry.utils.signing import unsign
from sentry.web.frontend.base import control_silo_view

from .integration_extension_configuration import IntegrationExtensionConfigurationView

# 24 hours to finish installation
INSTALL_EXPIRATION_TIME = 60 * 60 * 24


@control_silo_view
class MsTeamsExtensionConfigurationView(IntegrationExtensionConfigurationView):
    provider = "msteams"
    external_provider_key = "msteams"

    def map_params_to_state(self, params):
        # decode the signed params and add them to whatever params we have
        params = params.copy()
        signed_params = params["signed_params"]
        del params["signed_params"]
        params.update(
            unsign(
                signed_params,
                max_age=INSTALL_EXPIRATION_TIME,
                salt=SALT,
            )
        )
        return params
