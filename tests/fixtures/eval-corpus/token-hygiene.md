# Keeping a secret token safe

Never in the repo. Put it in a file with mode 600 under ~/.config, read it at runtime, rotate it if it ever lands in a log.
