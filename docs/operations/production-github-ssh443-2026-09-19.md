# Production: восстановление GitHub SSH через порт 443

Статус: archive — отчёт о выполненной операции 2026-09-19.
Состояние ниже подтверждено проверками в момент операции; перед диагностикой
следует повторить проверки. Документ не является автоматическим deploy-скриптом.

## Причина и решение

Соединение production с github.com:22 завершалось timeout до авторизации.
Официальный endpoint ssh.github.com:443 доступен. Временные Git bundle
заменены прямым SSH-доступом. Production Git имеет версию 1.8.3.1:
не поддерживает git -C и core.sshCommand.

Рабочая копия: /opt/clock-erp. Сервис: clock-erp. Команды ниже рассчитаны
на root на production; секретные файлы читать или копировать не требуется.

## Что изменено

В /opt/clock-erp/.git/config изменён только remote.origin.url:

- До: `git@github.com:vechasu/clock-erp.git`
- После: `ssh://git@ssh.github.com:443/otve4ai/clock-erp.git`

В /root/.ssh/config перед существующим блоком Host github.com добавлен:

```sshconfig
Host ssh.github.com
    IdentityFile /root/.ssh/id_ed25519_github_clock_erp
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Это существующий deploy key; его содержимое не публикуется.
Первичная проверка без явного ключа завершилась Permission denied (publickey):
нестандартное имя не входило в список автоматически выбираемых identity.
С явным ключом, а затем с новым Host-блоком GitHub ответил:

```text
Hi otve4ai/clock-erp! You've successfully authenticated, but GitHub does not provide shell access.
```

Для ssh -T этот успешный ответ сопровождается exit code 1: GitHub не предоставляет shell.

В /root/.ssh/known_hosts добавлена проверенная Ed25519-запись
[ssh.github.com]:443. SSH также автоматически добавил host key для
[140.82.121.35]:443 при CheckHostIP=yes; IP может изменяться и не является
постоянным адресом endpoint. Права /root/.ssh установлены 700,
known_hosts и config — 600.

Fingerprint полученного через ssh-keyscan публичного ключа вычислен
ssh-keygen -lf ... -E sha256 и сопоставлен с официальной документацией:

```text
SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
```

Источники:
- [GitHub SSH fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)
- [SSH через HTTPS-порт](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port)

Не использовать StrictHostKeyChecking=no. При изменении host key сначала
проверять актуальную официальную документацию, а не автоматически удалять запись.

## Проверенный результат

- Обычный git fetch origin выполнен успешно без временного SSH wrapper.
- origin/main доступен: 2fed492d00bd2242e20d8a2d1c7416fb4afb0048.
- HEAD до и после: 2fed492d00bd2242e20d8a2d1c7416fb4afb0048.
- Рабочая директория чиста вне штатных runtime-файлов instance/.
- clock-erp до и после: active.
- /opt/clock-erp-current сохранил цель
  /opt/clock-erp-releases/2fed492d00bd2242e20d8a2d1c7416fb4afb0048.
- Fetch обновил remote-tracking refs и Git-объекты.
- Код, .env, данные приложения и сервис не изменялись.
  Merge, reset, checkout, pull, deploy и перезапуск не выполнялись.
- Проверки HTTP и бизнес-сценариев в рамках этой операции не выполнялись.

## Диагностика без изменения приложения

```bash
cd /opt/clock-erp
git config --get remote.origin.url
git rev-parse HEAD
git status --porcelain --untracked-files=all
systemctl is-active clock-erp
readlink /opt/clock-erp-current
ssh -G -p 443 git@ssh.github.com | grep -E '^(hostname|port|identityfile|identitiesonly|stricthostkeychecking) '
ssh-keygen -F '[ssh.github.com]:443' -f /root/.ssh/known_hosts
ssh -n -T -p 443 -o BatchMode=yes -o ConnectTimeout=15 git@ssh.github.com
git ls-remote origin refs/heads/main
```

При анализе git status только штатные файлы внутри instance/ допустимо
исключать; остальные изменения нужно расследовать.
git ls-remote проверяет доступ без обновления локальных refs.
git fetch origin обновляет Git-данные, но не рабочие файлы и не HEAD.

Timeout означает проблему соединения; Permission denied (publickey) —
проблему выбора или прав deploy key. Repository not found после успешной
SSH-авторизации требует проверки URL и доступа ключа к репозиторию.

Host-блок действует на все подключения root к ssh.github.com, не только
на эту рабочую копию. Другому репозиторию на том же endpoint может понадобиться
отдельный SSH alias и собственный ключ. Существующий Host github.com сохранён.

## Резервная копия и откат

Перед редактированием сохранён каталог с правами 700:

```text
/root/clock-erp-git443-backup.ujqvKEMi/
    origin.url
    commit
    current-link
    ssh-config
```

Точный откат только remote (совместим со старым Git):

```bash
git --git-dir=/opt/clock-erp/.git config remote.origin.url 'git@github.com:vechasu/clock-erp.git'
```

Он возвращает зависимость от заблокированного порта 22.
При необходимости вернуть также прежнюю SSH-конфигурацию сначала убедиться,
что после операции в ней не было новых изменений:

```bash
cp -p /root/clock-erp-git443-backup.ujqvKEMi/ssh-config /root/.ssh/config
```

Если есть последующие изменения, удалять только добавленный Host-блок вручную,
не перезаписывать весь файл. Проверенные host keys можно оставить.
Резервная копия не содержит прежних прав каталога .ssh или known_hosts;
данные команды не отменяют chmod и добавление host keys.
Откат Git-доступа не требует перезапуска ERP.

Публикация этого отчёта не применяет серверную конфигурацию автоматически
и не меняет текущую production-версию.
