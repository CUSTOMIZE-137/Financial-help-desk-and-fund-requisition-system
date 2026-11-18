import { defineConfig } from "@neon/config/v1";

export default defineConfig({
  // Declare your Neon services here
  auth: false,
  // Branch policy: per-branch tuning
  branch: (branch) => {
    if (
      branch.isDefault ||
      branch.name === "staging" ||
      branch.name === "preview"
    ) {
      // Default and persistent pre-production (staging/preview) branches: no TTL override
      return {};
    }
    if (!branch.exists) {
      // Ephemeral feature/PR preview branches: auto-expire after 7 days
      // Run `neon checkout <name>` to create a new branch with these settings
      return { ttl: "7d" };
    }
    // Existing branch: no changes
    return {};
  },
});
