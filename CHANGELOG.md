# Zmiany

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
