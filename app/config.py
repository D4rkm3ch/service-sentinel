# How many registries to check at once. Registry checks are almost pure network
        # wait time, so doing them one at a time is what made a large stack slow to check.
        self.registry_check_concurrency: int = int(os.environ.get("REGISTRY_CHECK_CONCURRENCY", "10"))
