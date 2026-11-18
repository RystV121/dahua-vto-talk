<img src="custom_components/dahua_vto_talk/brand/icon.png" width="96" align="right" alt="">

# Dahua VTO Talk – rozmowa w dwie strony z domofonem Dahua w Home Assistant

Integracja dodaje do Home Assistanta brakującą połowę domofonu: **Twój głos do
furtki**. Obraz i dźwięk z furtki dalej bierzesz z kamery, którą już masz
(integracja Dahua od rrollera). Ta integracja mówi do domofonu sama, bez go2rtc,
bez serwera SIP i bez Asteriska.

Zrobiona pod **Dahua VTO2202F-P** (firmware 4.511), ale powinna działać z każdym
VTO, który ma kanał zwrotny ONVIF (VTO2202F-P-S2/S3, VTO2211G, VTO2311R…).

## Co rozwiązuje (dlaczego wcześniej nie działało)

1. **Kodek.** Twój VTO wysyła i przyjmuje dźwięk jako `L16/16000` (PCM). W tym
   formacie połączenie się zestawia, ale **głośnik przy furtce milczy**. Działa
   tylko G.711A 8 kHz. Do tego VTO **sam wraca do PCM** po kilku minutach (było
   to widać w nocy: zmiana na G.711A, a po 2 minutach znowu L16).
   → Integracja ustawia G.711A tuż **przed każdą rozmową**, sprawdza, co domofon
   faktycznie oferuje, i dodatkowo pilnuje kodeka w tle (strażnik co 30 s).
2. **go2rtc nie umie mówić w L16.** Nawet gdy wszystko wygląda dobrze,
   przeglądarka nie potrafi wysłać L16, a go2rtc go nie przekoduje.
   → Integracja ma **własne połączenie RTSP** z domofonem i **sama koduje**
   dźwięk w formacie, którego chce VTO.
3. **Kanał mówienia może mieć tylko jeden klient naraz.** Jeśli podgląd kamery
   w HA otwiera kanał zwrotny, nikt inny nie może mówić.
   → Instrukcja niżej mówi, co wyłączyć. Integracja pokazuje czytelny błąd
   „kanał mówienia zajęty”.

Jak to działa:

```
iPad / telefon (karta)          Home Assistant                       VTO przy furtce
mikrofon → AudioWorklet  ──►  websocket (binarnie, jak Assist) ──►  RTSP DESCRIBE/SETUP/PLAY
48 kHz → 8 kHz PCM16           koder G.711A, równe tempo            trackID=5 (a=sendonly)
                               pakiety jak go2rtc (1024 B)    ──►  głośnik
```

## Instalacja

**Przez HACS (zalecane, aktualizacje jednym kliknięciem):**
1. HACS → ⋮ (prawy górny róg) → **Niestandardowe repozytoria**.
2. Repozytorium: `https://github.com/RystV121/dahua-vto-talk`, typ: **Integration** → Dodaj.
3. Znajdź „Dahua VTO Talk” → **Pobierz**.
4. Uruchom ponownie Home Assistanta.

**Ręcznie:** skopiuj folder `custom_components/dahua_vto_talk` do
`/config/custom_components/` (Samba, Studio Code Server, SSH) i uruchom HA ponownie.

**Konfiguracja:**

1. **Ustawienia → Urządzenia i usługi → Dodaj integrację → „Dahua VTO Talk”.**
   - Adres: IP domofonu (np. `192.168.1.108`)
   - Użytkownik i hasło: te same co do strony www domofonu (`admin` …)
   - Port RTSP `554`, port HTTP `80`, kanał `1`

   Integracja od razu sprawdza, czy domofon wystawia kanał do mówienia. Jeśli nie,
   powie to wprost.
2. **W integracji Dahua (rroller)** → urządzenie Domofon → Konfiguruj: włącz
   **„Don't open the two-way audio channel when streaming”**. Dzięki temu podgląd
   kamery nie zajmuje kanału mówienia. (Wbudowany przycisk mówienia z Labs zniknie
   i dobrze, bo zastępuje go ta integracja.)
3. Na kamerze domofonu (integracja Dahua) **wyłącz „Preload stream”**: ciągle otwarty podgląd potrafi trzymać kanał domofonu.

## Pierwszy test (2 minuty, zrób to najpierw)

Urządzenie „Domofon – rozmowa” ma przycisk **„Ton testowy”**. Naciśnij go i
posłuchaj przy furtce (albo połóż tam telefon z dyktafonem):

| Co słychać przy furtce | Co to znaczy |
|---|---|
| 3 sekundy pikania | **Działa.** Przejdź do karty. |
| Nic | Spróbuj **„Ton testowy (pakiety 20 ms)”**, potem **„Ton testowy (HTTP postAudio)”**. Napisz, który zadziałał. |
| Szum / trzaski zamiast pikania | Domofon został na L16. Sprawdź sensor „Format ścieżki mówienia” (powinno być `PCMA/8000`) i naciśnij „Ustaw kodek G.711A teraz”. |
| Błąd „kanał mówienia jest zajęty” | Zamknij DMSS i podgląd z kanałem zwrotnym (punkt 4 instalacji). |

Przydatne sensory (diagnostyczne):
- **Rozmowa**: bezczynny / łączenie / mówisz / błąd (w atrybutach ostatni błąd, liczba pakietów, poziom mikrofonu),
- **Kodek audio strumienia głównego**: powinno być `G.711A/8000`,
- **Format ścieżki mówienia**: powinno być `PCMA/8000`,
- **Powroty kodeka**: ile razy domofon sam wrócił do PCM (strażnik naprawia).

## Karta na dashboard

Dashboard → Edytuj → Dodaj kartę → Ręcznie:

```yaml
type: custom:dahua-vto-talk-card
camera_entity: camera.domofon_main          # kamera z integracji Dahua
open_door_entity: button.domofon_open_door  # przycisk "Open Door" z integracji Dahua
mode: toggle            # toggle = klik start/stop, hold = trzymaj, żeby mówić
mute_while_talking: false
```

Nazwy encji sprawdź u siebie (Ustawienia → Urządzenia → Domofon). Karta ładuje
się sama, nie trzeba dodawać jej w „Zasobach”.

Na karcie: obraz z furtki, okrągłe przyciski **Dźwięk** (domyślnie wyłączony, żeby
domofon nie szumiał w tle), **Mów**, **Otwórz furtkę**, pasek poziomu mikrofonu i
status (`Mówisz • PCMA/8000 • pakiety: …`). Gdy ktoś dzwoni, karta pokazuje
„Ktoś dzwoni do furtki” i przyciski **Odbierz / Odrzuć** (patrz niżej).

### Osobny widok „Domofon” + przycisk na ekranie głównym

Dashboard → Edytuj → ⋮ → Edytor kodu, dopisz widok (`panel: true` = karta na
całą szerokość, wygodne na iPadzie):

```yaml
  - title: Domofon
    path: domofon
    icon: mdi:doorbell-video
    panel: true
    cards:
      - type: custom:dahua-vto-talk-card
        title: Furtka
        camera_card:
          type: custom:webrtc-camera
          url: domofon_sub
          muted: true
        open_door_entity: button.domofon_open_door
```

i na ekranie głównym kafelek, który do niego prowadzi:

```yaml
type: button
name: Domofon
icon: mdi:doorbell-video
tap_action:
  action: navigate
  navigation_path: domofon      # ścieżka widoku z powyższego
```

### Lżejszy, płynny podgląd (WebRTC + podstrumień)

Wbudowany podgląd HA bywa ciężki (strumień główny, często H.265) i na iPadzie
potrafi pokazywać tylko stopklatkę. Lepiej działa podstrumień (subtype=1,
H.264, mała rozdzielczość) puszczony przez go2rtc / WebRTC. Mając zainstalowaną
integrację **WebRTC Camera** (AlexxIT), wstaw ją do środka karty:

```yaml
type: custom:dahua-vto-talk-card
camera_card:
  type: custom:webrtc-camera
  url: rtsp://admin:HASLO@192.168.1.108:554/cam/realmonitor?channel=1&subtype=1#backchannel=0
  muted: true
open_door_entity: button.domofon_open_door
```

Dźwięk w podglądzie: domyślnie podstrumień VTO ma dźwięk wyłączony albo w PCM
16 kHz, którego przeglądarka przez WebRTC nie odtworzy. Integracja sama ustawia
podstrumieniowi dźwięk **włączony, G.711A 8 kHz** (opcja „Dźwięk podstrumienia
też w G.711A”, domyślnie włączona). Dźwięk włączasz przyciskiem 🔊 na karcie.

**Dźwięk podglądu poza domem (iPad / Safari):** gdy WebRTC nie może się połączyć
(np. przez internet, z innej sieci), karta WebRTC przechodzi na MSE. go2rtc
pakuje wtedy dźwięk G.711 w FLAC, którego Safari w MSE nie odtwarza: obraz jest,
dźwięku nie ma (sprawdzone testem `safari-mse`). Rozwiązanie: w `go2rtc.yaml`
(ten sam folder co `configuration.yaml`) zdefiniuj strumień, który ma dźwięk
także w AAC:

```yaml
streams:
  domofon_sub:
    - rtsp://admin:HASLO@192.168.1.108:554/cam/realmonitor?channel=1&subtype=1#backchannel=0
    - ffmpeg:domofon_sub#audio=aac
```

i w karcie użyj `url: domofon_sub`. WebRTC dalej dostaje G.711, MSE (Safari) –
AAC. Po zmianie zrestartuj HA (albo dodatek go2rtc).

**Dlaczego podgląd startuje w MSE, a po „Mów” przechodzi na RTC:** Safari
ukrywa adres iPada przed WebRTC, dopóki strona nie użyje mikrofonu. Karta
obchodzi to sama (od 0.1.8): jeśli aplikacja ma już zgodę na mikrofon, przełącza
podgląd na RTC zaraz po otwarciu, a jeśli nie – po stuknięciu 🔊.

`#backchannel=0` jest ważne: go2rtc nie zajmie wtedy kanału mówienia, z którego
korzysta przycisk **Mów**. Zamiast pełnego adresu możesz też podać nazwę
strumienia z `go2rtc.yaml` (np. `url: domofon_sub`).

### Czy mój głos dochodzi do furtki?

Po zakończeniu mówienia karta pokazuje `Wysłano 6.2 s • max głośność 35%` i
przycisk **▶ Odsłuchaj, co poszło do furtki**: odtwarza dokładnie ten dźwięk,
który integracja wysłała do domofonu. Słychać siebie wyraźnie = po stronie HA
wszystko gra. Jeśli głośność jest bliska 0%, mikrofon iPada dawał ciszę
(uprawnienia aplikacji HA do mikrofonu albo spróbuj w Safari).

### Czujniki do szukania problemów

* **Raport z karty: mówienie** – po każdym mówieniu z karty: `OK: 5.2 s, max 30%`
  albo gdzie dźwięk zginął (`mikrofon dawał samą ciszę`, `dźwięk nie dotarł do
  Home Assistanta`, …). W atrybutach: co widziała przeglądarka (mikrofon,
  AudioContext, liczba ramek) i co dostał HA (sekundy, poziom, pakiety).
* **Raport z karty: podgląd** – po stuknięciu 🔊: tryb odtwarzacza (RTC/MSE),
  czy jest ścieżka dźwięku, czy wideo jest wyciszone.
* **Dźwięk podstrumienia (podgląd)** – co domofon naprawdę wysyła w subtype=1.

Wszystko to jest też w diagnostyce urządzenia (⋮ → Pobierz diagnostykę).

### Opcja: przed mówieniem zakończ dzwonienie

Ustawienia → Dahua VTO Talk → Konfiguruj → **„Przycisk kończący połączenie VTO”**
→ wybierz `button.domofon_cancel_call`. Integracja naciśnie go przed otwarciem
kanału mówienia (tak robi działający setup felipecrs na tym samym modelu).
Uwaga: zakończy to też dzwonienie w DMSS na telefonach.

## iPad: aplikacja HA czy Safari?

Mikrofon w przeglądarce wymaga **HTTPS** (np. `https://twoj-ha.example.com` albo Nabu Casa).

- **Aplikacja Home Assistant**: najpierw spróbuj tu. Przy pierwszym „Mów” iOS
  powinien zapytać o mikrofon. Jeśli karta pokaże „Brak dostępu do mikrofonu”:
  iPad → Ustawienia → Home Assistant → Mikrofon → włącz.
- **Plan B: Safari.** Otwórz HA w Safari → Udostępnij → **Do ekranu początkowego**.
  Taka „aplikacja” na pewno dostaje mikrofon (to standard WebKit). Na wiszącego
  iPada włącz do tego **Dostęp z przewodnikiem**.
- **Brave na komputerze** blokował dźwięk w nocy. Do HA używaj Chrome albo
  wyłącz tarcze Brave dla adresu HA.

## Okienko „Ktoś dzwoni” (iPad na ścianie i każdy inny ekran)

Bez dodatkowych dodatków (browser_mod niepotrzebny). Gdy ktoś zadzwoni, na
**każdym otwartym ekranie Home Assistanta** wyskakuje okienko z podglądem:

* **Odbierz** – włącza mikrofon i dźwięk z furtki (jedno stuknięcie),
* **Mikrofon** – wycisza / włącza Twój mikrofon w trakcie rozmowy,
* **Otwórz furtkę**,
* **Rozłącz** – kończy mówienie, wycisza podgląd i zamyka okienko,
* **Odrzuć** – zamyka okienko bez odbierania.

Gdy ktoś odbierze na jednym ekranie, na pozostałych okienko znika („Odebrano na
innym urządzeniu”). Samo zamyka się po czasie z opcji (domyślnie 60 s).

Ustawienie (raz):

1. **Ustawienia → Urządzenia i usługi → Dahua VTO Talk → Konfiguruj → „Encja
   dzwonka”**: wybierz encję, która mówi, że ktoś nacisnął przycisk na
   domofonie (np. czujnik / zdarzenie przycisku z integracji Dahua). Zamiast
   tego możesz też w dowolnej automatyzacji wywołać `dahua_vto_talk.ring`:

   ```yaml
   alias: Domofon – okienko
   triggers:
     - trigger: state
       entity_id: binary_sensor.domofon_button_pressed   # Twoja encja dzwonka
       to: "on"
   actions:
     - action: dahua_vto_talk.ring
   ```

2. Na iPadzie **otwórz raz widok z kartą** – okienko wygląda potem tak samo jak
   ta karta (ta sama kamera, ten sam przycisk furtki).
3. Dzwoneczek w rogu karty (**okienko / bez okienka**) włącza lub wyłącza
   okienko na danym urządzeniu (np. wyłącz na telefonie, zostaw na iPadzie).

Test bez wychodzenia z domu: **Narzędzia deweloperskie → Akcje →
`dahua_vto_talk.ring`**. Czujnik **„Połączenie”** (brak / dzwoni / odebrane)
nadaje się do własnych automatyzacji (np. podświetl ekran, wycisz muzykę).
Okienko pojawia się, gdy aplikacja HA jest otwarta na ekranie.

## Usługi (np. do automatyzacji)

- `dahua_vto_talk.play_test_tone`: ton testowy (`seconds`, `frequency`, `transport: rtsp|http`, `packet_bytes`, `zero_timestamp`)
- `dahua_vto_talk.play_media`: odtwórz plik / URL / **TTS** w głośniku domofonu, np. „Paczkę proszę zostawić przy furtce”
- `dahua_vto_talk.fix_codec`: ustaw G.711A i zwróć, co domofon oferuje
- `dahua_vto_talk.stop`: zamknij kanał mówienia
- `dahua_vto_talk.ring`: pokaż okienko „Ktoś dzwoni” na wszystkich ekranach
- `dahua_vto_talk.dismiss`: zamknij okienko / zakończ dzwonienie

## Ustawienia (Konfiguruj)

| Opcja | Domyślnie | Po co |
|---|---|---|
| Ustaw G.711A przed każdą rozmową | tak | Najważniejsza poprawka. |
| Przywróć oryginalny kodek po rozmowie | nie | Gdy DMSS trzeszczy na G.711A: kodek jest G.711A tylko na czas rozmowy z HA. Wyłącza strażnika. |
| Co ile sekund pilnować kodeka | 30 | Strażnik w tle (przełącznik „Pilnuj kodeka G.711A”). |
| Rozmiar pakietu RTP | 1024 | Tyle wysyła go2rtc do domofonów Dahua. Mniej = mniejsze opóźnienie (spróbuj 320). |
| Znacznik czasu RTP zawsze 0 | tak | Jak go2rtc. |
| L16 little-endian | nie | Tylko jeśli domofon uparcie zostaje na L16. |
| Zestawiaj też ścieżkę mikrofonu domofonu | nie | Gdyby jakiś firmware nie przyjmował samej ścieżki do mówienia. |

## Jak to było testowane (bez Twojego domofonu)

- **Atrapa VTO2202F** odtwarzająca jego zachowanie z nocy: dokładnie to samo SDP,
  digest RTSP ważny tylko na jednym połączeniu, `Content-Base` zakończone
  `proto=Onvif/`, start w PCM 16 kHz, samoczynny powrót do PCM, jeden mówiący
  naraz, a głośnik gra tylko G.711 8 kHz.
- **Prawdziwe go2rtc 1.9.15** przeciw atrapie: dogaduje się tak jak z prawdziwym
  Dahua i przy L16 też jest cisza, więc atrapa jest wierna. Pakiety go2rtc
  (1024 B, znacznik czasu 0, marker) są identyczne z pakietami tej integracji,
  ta sama sekwencja żądań RTSP.
- **testy automatyczne** w prawdziwym Home Assistant (config flow, opcje,
  strażnik, ton testowy, websocket, zajęty kanał, złe hasło, rozłączenie,
  przywracanie kodeka, diagnostyka).
- **Pełny test w prawdziwym Chrome**: uruchomiony Home Assistant z frontendem,
  karta na dashboardzie, udawany mikrofon grający 600 Hz, prawdziwe kliknięcie
  „Mów”. Atrapa domofonu „usłyszała” 600 Hz w PCMA, a po „Stop” kanał się zwolnił.
  Ten test wyłapał błąd, przez który karta by się nie pokazała („Configuration
  error”); jest poprawiony.

Nie było testu na prawdziwym domofonie. To zrobimy razem: zacznij od „Tonu
testowego”.

## Diagnostyka do wysłania

Ustawienia → Urządzenia → „Domofon – rozmowa” → ⋮ → **Pobierz diagnostykę**.
Plik zawiera aktualne ustawienia audio z domofonu i pełną odpowiedź RTSP (bez hasła).

## Aktualizacje

HACS sam pokaże „Dostępna aktualizacja”, gdy pojawi się nowe wydanie. Kliknij
Aktualizuj, uruchom HA ponownie i przeładuj aplikację/przeglądarkę (karta
aktualizuje się razem z integracją). Lista zmian: [CHANGELOG.md](CHANGELOG.md).

## Testy (dla programistów)

Podgląd z dźwiękiem w prawdziwej przeglądarce (go2rtc + WebRTC Camera + HA +
Chrome, analiza dźwięku z `<video>` po kliknięciu 🔊):
`tests/e2e/preview_audio_e2e.py` (opis uruchomienia w nagłówku pliku).

```
pip install pytest-homeassistant-custom-component pytest-timeout
pytest tests                                   # testy integracji, w tym atrapa VTO
GO2RTC_BIN=/ścieżka/go2rtc pytest tests        # + porównanie z prawdziwym go2rtc
npm i jsdom && node tests/js/card.test.js      # karta: resampler, przyciski, kamera
```
