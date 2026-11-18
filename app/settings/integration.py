# Add your integration-specific settings here
from environs import Env

env = Env()

# Read timeout for EarthRanger calls; erclient's default is 20 s, and the v1 sharing jobs used 60 s.
ER_DATA_TIMEOUT_SECONDS = env.float("ER_DATA_TIMEOUT_SECONDS", 60.0)
