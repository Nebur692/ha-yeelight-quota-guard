<div align="center">

<img src="custom_components/yeelight_quota_guard/brand/icon.png" width="96" alt="">

# ha-yeelight-quota-guard

*Keeps your Yeelight bulbs out of the 49.7-day firmware lock — they restart themselves before Home Assistant ever says "try power cycling the device"*

![Release](https://img.shields.io/github/v/release/Nebur692/ha-yeelight-quota-guard?label=release&color=blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-30363D?logo=githubsponsors&logoColor=EA4AAA)](https://github.com/sponsors/Nebur692)
[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/nebur69265723)
[![PayPal](https://img.shields.io/badge/PayPal-donate-00457C?logo=paypal&logoColor=white)](https://paypal.me/0SkillS)

🇬🇧 [English](#english) · 🇪🇸 [Español](#español)

</div>

---

## English

### ✨ What this is

If your Yeelight bulbs show this in Home Assistant, all at once and out of nowhere:

> Could not fetch initial state; try power cycling the device

and their automations fail with *"Timed out when calling _async_turn_on"*, it is very likely
not your Wi-Fi. It is a **firmware bug**: once a Yeelight has been powered for **2³² milliseconds
— 49 days, 17 hours and 3 minutes —** the timer that refills its LAN command quota stops working.
From then on every command, even a plain read, gets `client quota exceeded`, until someone cuts
its power. Yeelight [acknowledged it in 2022](https://forum.yeelight.com/t/topic/15756) and
never shipped a fix.

Bulbs that came back together after a power cut also lock together, to the minute, 49.7 days
later. And the official advice, "power cycle it", means climbing to every ceiling light.

**This integration fixes it without touching anything.** The bulbs have a second, separate
control protocol (miIO, the one the Mi Home app uses) that the bug does not reach. Through it
the guard:

- reads how long each bulb has been powered (`life`, in seconds);
- **restarts** it (`miIO.reboot`) — the bulb comes back in about ten seconds, **with the same
  on/off state**, and with a fresh quota;
- does it **before** the lock, inside a quiet window and only while the light is off, or
  **straight away** if a bulb is already locked: past the limit **and** Home Assistant can no longer
  talk to it. Some models (light strips, the older color bulbs) pass the limit without locking;
  those are never restarted in the middle of the day for nothing. Once a bulb is seen still
  working 10 minutes past the limit — asked the same way Home Assistant asks it — it is
  remembered as **not affected** on that firmware, and is no longer restarted at night either
  (a firmware update makes it learn again);
- leaves out any bulb whose Yeelight integration entry you have **disabled**;
- tells Home Assistant's Yeelight integration to retry at once instead of waiting out its
  back-off.

**Measured, not assumed** — on a real home with 40 Yeelight bulbs, on 15 October 2026:

| | |
|---|---|
| Bulbs locked at the same time | 16, all with **50.09 days** of uptime |
| Locked firmwares | `colorc` 2.0.8_0016, `panel1` 2.0.6_0021, `ceiling15` 2.0.6_0023, `color4` 2.1.7_0039 |
| Past 50 days with no lock | `strip2` 2.0.6_0073, `color2` 2.0.6_0065, `strip6` 2.1.7_0020 |
| Restarted with `miIO.reboot` | 16 of 16, every one with its on/off state kept; Home Assistant picked up 14 on its own and 2 after reloading their entry, which the guard now does by itself |

### 📦 Installation

You need **Home Assistant 2026.9 or newer** and your bulbs already set up with Home Assistant's
own **Yeelight** integration: the guard watches the bulbs that integration knows.

**With HACS (recommended)**

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/Nebur692/ha-yeelight-quota-guard`,
   category **Integration**.
2. Search for **Yeelight Quota Guard**, download it, and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Yeelight Quota Guard*.

**By hand**

Copy `custom_components/yeelight_quota_guard` into your `config/custom_components/` folder and
restart Home Assistant.

### ⚙️ Configuration

The guard needs each bulb's **miIO token** (32 hexadecimal characters). There are two ways:

- **From your Xiaomi account (recommended).** The account the bulbs were added with in Mi Home or
  the Yeelight app, and its server region (Europe is usually *Germany*). All tokens arrive at once.
  **The password is used for that single request and is never stored**; only the tokens are kept,
  like Home Assistant's own Xiaomi Miio integration does. Xiaomi often asks for more on a new
  sign-in, and the wizard asks for it in turn: a **captcha** (type the characters in the picture)
  and a **code sent by email or SMS**. Then a list shows every bulb found, one per line and all
  ticked: untick any you want to leave out, and the bulbs missing from the account are listed apart.
- **By hand**, bulb by bulb. The bulb is asked straight away, so a wrong token is caught on the spot.

Tokens are tied to bulbs by their **device id**, never by IP address, and every request checks
that the device answering is the right one: a restart can never land on another device.

Afterwards, **Configure** on the integration offers:

| Option | Default | What it does |
|---|---|---|
| Repair locked bulbs | on | A bulb past 49.7 days that Home Assistant can no longer talk to (its Yeelight entry asks to power cycle it, or its light has been unavailable for two checks in a row) is restarted at once, at any hour: it no longer obeys Home Assistant anyway. So is a younger bulb whose Yeelight entry has asked to power cycle it for two checks in a row while it does not answer on the LAN either (seen after a Home Assistant restart: the bulb never let it back in). A bulb already seen working past the limit is only restarted if it answers "quota exceeded" itself or hangs like that: if it just goes unavailable, the cause is something else and a restart does not cure it. After any restart, if Home Assistant still shows the light unavailable, its Yeelight entry is reloaded |
| Restart before they lock | on | Bulbs close to the limit are restarted inside the quiet window |
| Restart after this many days powered on | 45 | Leaves several nights to find the light off |
| Quiet window | 03:00 – 05:00 | Can cross midnight; the same start and end means all day |
| Only while the light is off | on | A restart keeps the light as it was, but this avoids touching a light in use |
| Check every | 15 min | One small UDP exchange per bulb |

The same menu lets you add or replace a token by hand, or fetch them again from the cloud.

### 🧭 Usage

Each guarded bulb gets three entities, on its own device page, which takes the area the bulb already has in the Yeelight integration (and follows it if you move the bulb there):

| Entity | |
|---|---|
| **Powered on since** | When the bulb last started |
| **Quota lock expected** | When it would lock if nothing restarted it. *Unknown* for a bulb seen working past the limit: its *Affected by the lock* attribute says *No* |
| **Restart** | Restart it now over miIO — works even while it is locked |

Every restart shows in the **logbook**, on the bulb's device page, with why it was restarted and how it came back.
It also fires the event `yeelight_quota_guard_restarted`, with `bulb`, `device_id`, `reason`
(`repair`, `prevent` or `manual`) and `result` (`ok`, `power_restored`, `not_back`, …).
For example, to get a phone notification:

```yaml
triggers:
  - trigger: event
    event_type: yeelight_quota_guard_restarted
actions:
  - action: notify.mobile_app_my_phone
    data:
      message: "{{ trigger.event.data.bulb }} restarted ({{ trigger.event.data.reason }})"
```

Problems that need you appear in **Settings → Repairs**, each saying whether you need to act
and with its *Burburito code*:

- *Yeelight bulbs stuck with no token* — Home Assistant shows the quota-lock symptom on bulbs
  the guard has no token for.
- *A bulb no longer accepts its token* — it was reset or added again in the app.
- *A bulb did not restart* — it accepted the order but did not come back with a fresh uptime.

### 🩹 Troubleshooting

- **"Those characters did not match"**: a new captcha comes with the error; type that one.
- **"Xiaomi did not accept that code"**: it is wrong or has expired (they last a few minutes).
  Start the wizard again to get a new one.
- **"Xiaomi wants to confirm this sign-in"**: on accounts with no email or phone to send a code
  to, Xiaomi offers a link instead. Open it, confirm the sign-in, and press Submit again.
- **"That account has no Yeelight bulb that Home Assistant knows"**: check the server region; the
  tokens live on the server the bulbs were added with.
- **A bulb shows as unavailable in the guard**: it did not answer over miIO — usually it is
  switched off at the wall. That is not an error, and nothing is reported.
- **Diagnostics** (the integration's ⋮ menu) include every bulb's state with the tokens redacted.

### 💙 Support

None of this would be possible without the community's support. If this project has been useful
to you, consider supporting it on [Ko-fi](https://ko-fi.com/nebur69265723) or
[PayPal](https://paypal.me/0SkillS) — every bit helps keep it maintained.

### 📜 License

[MIT](LICENSE).

### ⚠️ Disclaimer

Not affiliated with Yeelight, Xiaomi or Home Assistant. *Yeelight* and *Xiaomi* are trademarks of
their owners.

---

## Español

### ✨ Qué es

Si tus bombillas Yeelight muestran esto en Home Assistant, todas a la vez y sin motivo aparente:

> Could not fetch initial state; try power cycling the device

y sus automatizaciones fallan con *«Timed out when calling _async_turn_on»*, lo más probable es
que no sea tu WiFi. Es un **fallo del firmware**: cuando una Yeelight lleva **2³² milisegundos
encendida —49 días, 17 horas y 3 minutos—**, el temporizador que rellena su cupo de órdenes por
la red local deja de funcionar. A partir de ahí, cualquier orden, aunque sea solo leer su estado,
recibe `client quota exceeded`, hasta que alguien le quita la corriente. Yeelight
[lo reconoció en 2022](https://forum.yeelight.com/t/topic/15756) y nunca lo arregló.

Las bombillas que volvieron juntas después de un corte de luz se bloquean también juntas, al
minuto, 49,7 días después. Y el consejo oficial, «quítale la corriente», significa subirse a
cada plafón.

**Esta integración lo arregla sin tocar nada.** Las bombillas tienen un segundo protocolo de
control, aparte (miIO, el que usa la app Mi Home), al que el fallo no llega. Por él, el guardián:

- lee cuánto tiempo lleva encendida cada bombilla (`life`, en segundos);
- la **reinicia** (`miIO.reboot`): vuelve en unos diez segundos, **encendida o apagada como
  estaba**, y con el cupo nuevo;
- lo hace **antes** del bloqueo, dentro de una franja tranquila y solo con la luz apagada, o
  **en el momento** si una bombilla ya está bloqueada: ha pasado el límite **y** Home Assistant ya
  no puede hablar con ella. Algunos modelos (las tiras de luz, las bombillas de color antiguas)
  pasan el límite sin bloquearse; a esos nunca se les reinicia a mitad del día porque sí. Cuando
  una bombilla sigue funcionando 10 minutos después del límite (preguntándole igual que le
  pregunta Home Assistant), se recuerda como **no afectada** con ese firmware y tampoco se la
  reinicia de madrugada (si se actualiza el firmware, lo vuelve a aprender);
- deja fuera las bombillas cuya entrada de la integración Yeelight hayas **desactivado**;
- le pide a la integración Yeelight de Home Assistant que vuelva a intentarlo enseguida, en vez
  de esperar su tiempo entre reintentos.

**Medido, no supuesto**, en una casa real con 40 bombillas Yeelight, el 15 de octubre de 2026:

| | |
|---|---|
| Bombillas bloqueadas a la vez | 16, todas con **50,09 días** encendidas |
| Firmwares que se bloquean | `colorc` 2.0.8_0016, `panel1` 2.0.6_0021, `ceiling15` 2.0.6_0023, `color4` 2.1.7_0039 |
| Pasaron de 50 días sin bloquearse | `strip2` 2.0.6_0073, `color2` 2.0.6_0065, `strip6` 2.1.7_0020 |
| Reiniciadas con `miIO.reboot` | 16 de 16, todas con su estado intacto; Home Assistant recuperó 14 por su cuenta y 2 tras recargar su entrada, que ahora hace el guardián solo |

### 📦 Instalación

Necesitas **Home Assistant 2026.9 o posterior** y tus bombillas ya configuradas con la integración
**Yeelight** de Home Assistant: el guardián vigila las bombillas que conoce esa integración.

**Con HACS (recomendado)**

1. HACS → ⋮ → **Repositorios personalizados** → añade
   `https://github.com/Nebur692/ha-yeelight-quota-guard`, categoría **Integración**.
2. Busca **Yeelight Quota Guard**, descárgala y reinicia Home Assistant.
3. Ajustes → Dispositivos y servicios → **Añadir integración** → *Yeelight Quota Guard*.

**A mano**

Copia `custom_components/yeelight_quota_guard` en tu carpeta `config/custom_components/` y
reinicia Home Assistant.

### ⚙️ Configuración

El guardián necesita el **token miIO** de cada bombilla (32 caracteres hexadecimales). Hay dos
formas:

- **Desde tu cuenta de Xiaomi (recomendado).** La cuenta con la que se añadieron las bombillas en
  Mi Home o en la app de Yeelight, y la región de su servidor (en Europa suele ser *Alemania*).
  Llegan todos los tokens de una vez. **La contraseña solo se usa para esa consulta y no se
  guarda nunca**; solo se guardan los tokens, igual que hace la integración Xiaomi Miio de Home
  Assistant. Al iniciar sesión desde un sitio nuevo, Xiaomi suele pedir algo más y el asistente lo
  va pidiendo: un **captcha** (escribe los caracteres de la imagen) y un **código por correo o
  SMS**. Después sale una lista con todas las bombillas encontradas, una por línea y todas
  marcadas: desmarca las que quieras dejar fuera; las que faltan en la cuenta salen aparte.
- **A mano**, bombilla a bombilla. Se le pregunta a la bombilla en el momento, así que un token
  equivocado se detecta ahí mismo.

Cada token se asocia a su bombilla por el **id del aparato**, nunca por la IP, y cada petición
comprueba que contesta el aparato correcto: un reinicio nunca puede caer en otro.

Después, **Configurar** en la integración ofrece:

| Opción | Por defecto | Qué hace |
|---|---|---|
| Arreglar las bombillas bloqueadas | sí | Una bombilla que pasa de 49,7 días y con la que Home Assistant ya no puede hablar (su entrada de Yeelight pide quitarle la corriente, o su luz lleva dos comprobaciones seguidas no disponible) se reinicia en el momento, a cualquier hora: de todas formas ya no obedece a Home Assistant. También una más joven cuya entrada de Yeelight lleva dos comprobaciones seguidas pidiendo quitarle la corriente y que tampoco contesta por la red local (visto tras reiniciar Home Assistant: la bombilla no le dejaba volver a entrar). Una bombilla que ya se ha visto funcionar pasado el límite solo se reinicia si ella misma contesta «cupo excedido» o se cuelga así: si solo sale no disponible, la causa es otra y reiniciarla no lo arregla. Tras cualquier reinicio, si Home Assistant sigue viendo la luz no disponible, se recarga su entrada de Yeelight |
| Reiniciar antes de que se bloqueen | sí | Las que se acercan al límite se reinician dentro de la franja tranquila |
| Reiniciar tras estos días encendida | 45 | Deja varias noches para pillar la luz apagada |
| Franja tranquila | 03:00 – 05:00 | Puede cruzar la medianoche; el mismo inicio y fin significa todo el día |
| Solo con la luz apagada | sí | El reinicio deja la luz como estaba, pero así no se toca una luz que alguien está usando |
| Comprobar cada | 15 min | Un pequeño intercambio UDP por bombilla |

En el mismo menú se puede añadir o cambiar un token a mano, o volver a traerlos de la nube.

### 🧭 Uso

Cada bombilla vigilada tiene tres entidades, en su propia ficha de dispositivo, que toma el área que la bombilla ya tiene en la integración Yeelight (y la sigue si la cambias allí):

| Entidad | |
|---|---|
| **Encendida desde** | Cuándo arrancó la bombilla por última vez |
| **Bloqueo previsto** | Cuándo se bloquearía si nada la reiniciara. *Desconocido* en una bombilla que ya se ha visto funcionar pasado el límite: su atributo *Le afecta el bloqueo* dice *No* |
| **Reiniciar** | La reinicia ahora por miIO; funciona incluso estando bloqueada |

Cada reinicio aparece en el **registro**, en la ficha de la bombilla, con el motivo y cómo ha vuelto.
Además lanza el evento `yeelight_quota_guard_restarted`, con `bulb`, `device_id`, `reason`
(`repair`, `prevent` o `manual`) y `result` (`ok`, `power_restored`, `not_back`…).
Por ejemplo, para recibir un aviso en el móvil:

```yaml
triggers:
  - trigger: event
    event_type: yeelight_quota_guard_restarted
actions:
  - action: notify.mobile_app_mi_movil
    data:
      message: "{{ trigger.event.data.bulb }} reiniciada ({{ trigger.event.data.reason }})"
```

Lo que necesita que hagas algo aparece en **Ajustes → Reparaciones**, diciendo si tienes que
actuar y con su *Código burburito*:

- *Bombillas Yeelight atascadas y sin token*: Home Assistant muestra el síntoma del bloqueo en
  bombillas de las que el guardián no tiene token.
- *Una bombilla ya no acepta su token*: se restableció o se volvió a añadir en la app.
- *Una bombilla no se ha reiniciado*: aceptó la orden, pero no volvió con el contador a cero.

### 🩹 Solución de problemas

- **«Esos caracteres no coinciden»**: con el error llega un captcha nuevo; escribe ese.
- **«Xiaomi no ha aceptado ese código»**: no es correcto o ha caducado (duran unos minutos).
  Vuelve a empezar el asistente para que te mande otro.
- **«Xiaomi quiere confirmar este inicio de sesión»**: en cuentas sin correo ni teléfono al que
  mandar un código, Xiaomi ofrece un enlace. Ábrelo, confirma el acceso y vuelve a pulsar Enviar.
- **«Esa cuenta no tiene ninguna bombilla Yeelight de las que conoce Home Assistant»**: revisa la
  región del servidor; los tokens están en el servidor con el que se añadieron las bombillas.
- **Una bombilla sale como no disponible en el guardián**: no contestó por miIO; lo normal es que
  esté apagada desde el interruptor. No es un error y no se avisa de nada.
- **Diagnóstico** (menú ⋮ de la integración): incluye el estado de cada bombilla con los tokens
  tapados.

### 💙 Apoya el proyecto

Sin el apoyo de la comunidad estos proyectos no serían posibles. Si te ha resultado útil, puedes
apoyarlo en [Ko-fi](https://ko-fi.com/nebur69265723) o [PayPal](https://paypal.me/0SkillS) —
cualquier aportación ayuda a seguir manteniéndolo.

### 📜 Licencia

[MIT](LICENSE).

### ⚠️ Aviso

Sin relación con Yeelight, Xiaomi ni Home Assistant. *Yeelight* y *Xiaomi* son marcas de sus
respectivos dueños.
