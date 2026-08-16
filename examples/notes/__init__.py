"""A small application that is a config file.

Five modules and two YAML documents, arranged to demonstrate the claim the
whole architecture rests on: an application is data, and any implementation in
it can be replaced while it runs without a line of consumer code changing.

* :mod:`~examples.notes.store` is the **Definition** -- the contract, and the
  only module a consumer imports.
* :mod:`~examples.notes.providers` holds two **Providers** of it, one in
  memory and one on disk. Neither is imported by anything; the config file
  names them.
* :mod:`~examples.notes.consumers` holds two **Consumers**, which know the
  Definition and nothing else.
* :mod:`~examples.notes.journal` is a second service, so what the consumers
  saw is recorded rather than printed and can be asserted on.
* :mod:`~examples.notes.app` boots ``cordis.yml`` and runs the scenario.

``python -m examples.notes.app --help`` from the repository root.
"""

from __future__ import annotations
