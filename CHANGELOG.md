# Zmiany

## 0.2.1
- Okienko „Ktoś dzwoni” tylko na wybranych urządzeniach (domyślnie nigdzie):
  włącza je karta z `call_popup: true` (np. na dashboardzie panelu ściennego
  iPada) albo dzwoneczek na karcie. Telefony i inne tablety nie dzwonią.
  Okienko wygląda jak karta z `call_popup: true`.

## 0.2.0
- **Okienko „Ktoś dzwoni”** na każdym otwartym ekranie HA (bez browser_mod):
  Odbierz (mikrofon + dźwięk z furtki jednym stuknięciem), wyciszenie mikrofonu,
  Otwórz furtkę, Rozłącz (wycisza podgląd i zamyka okienko), Odrzuć. Gdy ktoś
  odbierze na jednym ekranie, na innych okienko znika. Wyzwalanie: „Encja
  dzwonka” w opcjach albo usługa `dahua_vto_talk.ring`; `dahua_vto_talk.dismiss`
  zamyka. Okienko można wyłączyć na danym urządzeniu (dzwoneczek na karcie).
- Czujnik „Połączenie” (brak / dzwoni / odebrane).
- Nowy wygląd karty: okrągłe przyciski z ikonami, status „na żywo / wyciszony”
  na podglądzie, licznik czasu rozmowy; dźwięk z furtki domyślnie wyłączony.
- Wyciszony mikrofon w rozmowie wysyła ciszę (bez dziur w dźwięku na furtce).
- Licencja MIT.

## 0.1.11
- Suwak „Wzmocnienie dźwięku podglądu” (0–24 dB, domyślnie 0): głośniejszy
  dźwięk z furtki w karcie (Web Audio + ogranicznik). Przy 0 dB odtwarzanie
  jest dokładnie takie jak wcześniej; jeśli przeglądarka nie pozwoli na
  wzmocnienie, karta sama wraca do zwykłego dźwięku. Działa dla podglądu
  WebRTC (RTC); w trybie MSE gra bez wzmocnienia. Można też ustawić w karcie:
  `preview_gain_db: 12`.

## 0.1.10
- Mówienie przez internet nie jest już poszarpane: bufor 300 ms zbiera dźwięk
  przed startem i po każdym przestoju sieci, zamiast wysyłać go co do ramki.
  Test z typowym „szarpanym” łączem (przestoje 150–250 ms): wcześniej 2 przerwy
  na głośniku (196 ms), teraz 0. Koszt: ok. 0,3 s opóźnienia.
- Licznik przerw: na karcie w trakcie mówienia („przerwy: N”) i w czujniku
  „Raport z karty: mówienie” (`underruns`, `gap_ms`).

## 0.1.9
- Suwak „Wzmocnienie mówienia” (0–24 dB, domyślnie 0): głośniej odtwarza Twój
  głos na domofonie, z miękkim ogranicznikiem (bez charczenia). Mowa z iPada
  jest ok. 15–20 dB cichsza niż ton testowy. Można zmieniać w trakcie rozmowy.

## 0.1.8
- Podgląd z WebRTC Camera sam przechodzi na WebRTC (RTC) zamiast MSE: Safari na
  iPadzie (i Chrome) ukrywa adres urządzenia przed WebRTC, dopóki strona nie
  użyje mikrofonu, więc poza domem WebRTC się nie łączył. Gdy aplikacja ma już
  zgodę na mikrofon, karta przełącza podgląd od razu po otwarciu; w przeciwnym
  razie po stuknięciu 🔊 (wtedy może poprosić o drugie stuknięcie, żeby włączyć
  dźwięk). Wyłączenie: `prefer_webrtc: false`.
- Testy w przeglądarce z symulacją iPada poza domem (oba przypadki).

## 0.1.7
- Mówienie na iOS: po otwarciu mikrofonu karta tworzy świeży AudioContext
  (WebKit potrafi podawać ciszę z kontekstu utworzonego wcześniej), a gdy przez
  2 s przychodzą same zera, sama odbudowuje przechwytywanie dźwięku.
- Czujniki „Raport z karty: mówienie” i „Raport z karty: podgląd”: gdzie ginie
  dźwięk (przeglądarka → HA → domofon), z atrybutami do diagnostyki.
- README: dźwięk podglądu w Safari poza domem (MSE nie odtwarza FLAC) –
  strumień go2rtc z dodatkowym AAC. Sprawdzone w przeglądarce (symulacja Safari).

## 0.1.6
- Po stuknięciu 🔊 karta pokazuje diagnostykę dźwięku podglądu: czy jest
  ścieżka dźwięku, czy wideo jest wyciszone, głośność, tryb (WebRTC/MSE)
  i czy gra.

## 0.1.5
- Nowy czujnik „Dźwięk podstrumienia (podgląd)”: pokazuje, czy podstrumień
  domofonu naprawdę ma dźwięk i w jakim formacie (np. `PCMA/8000` albo `none`).
- Podgląd z `camera_card` (np. WebRTC Camera) pojawia się także wtedy, gdy jego
  karta wczyta się później niż nasza (wcześniej trzeba było przeładować stronę).
- Przycisk „Odsłuchaj” nie pokazuje się, zanim cokolwiek powiesz.
- Nowy test w prawdziwej przeglądarce: dźwięk z podglądu WebRTC po kliknięciu 🔊
  (go2rtc + WebRTC Camera + Home Assistant + Chrome).

## 0.1.4
- Własna ikona integracji (widoczna w HA 2026.3+ na liście integracji i w HACS).
- Numeracja wersji na GitHubie: każda nowa wersja dostaje tag i wydanie (release),
  HACS pokazuje numer zamiast kodu zmiany.

## 0.1.3
- Dźwięk podstrumienia (subtype=1) ustawiany na G.711A 8 kHz, żeby podgląd
  WebRTC/go2rtc miał dźwięk (opcja, domyślnie włączona).
- Po mówieniu karta pokazuje, ile sekund wysłano i jak głośno, ostrzega przy
  ciszy z mikrofonu i ma przycisk „▶ Odsłuchaj, co poszło do furtki”.

## 0.1.2
- Opcja `camera_card`: dowolna karta kamery w miejscu podglądu (np. lekki
  podstrumień przez WebRTC Camera), czytelny komunikat przy złej encji kamery.

## 0.1.1
- Podgląd kamery startuje wyciszony (iOS nie odtwarza automatycznie wideo z dźwiękiem).

## 0.1.0
- Pierwsza wersja: mówienie przez kanał zwrotny RTSP/ONVIF, pilnowanie kodeka
  G.711A, karta Lovelace, ton testowy, usługi, diagnostyka.
