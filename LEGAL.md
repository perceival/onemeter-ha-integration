# Nota prawna

*English version below.*

To nie jest porada prawna. Poniżej opisujemy, czym jest ten projekt, co
zawiera i na jakich zasadach jest udostępniany.

## Brak powiązań

To niezależny projekt społecznościowy, **niezwiązany z OneMeter sp. z
o.o.**, jej masą upadłościową ani ewentualnym następcą prawnym, ani przez
nie wspierany. Nazwa „OneMeter” oraz powiązane oznaczenia i logotypy
należą do ich właścicieli. Używamy ich wyłącznie, by wskazać, z jakim
sprzętem to oprogramowanie współpracuje.

## Po co i jak powstał

Czytnik OneMeter wymaga chmury producenta, którą wyłączono po upadłości
firmy. Sprawne urządzenia stały się dla właścicieli bezużyteczne. Ta
integracja przywraca ich **współdziałanie** z Home Assistantem.

Protokół został ustalony samodzielnie przez autorów projektu (w tym
autora oryginalnej integracji) na podstawie obserwacji, badania i
testowania urządzeń będących ich własnością, a w niezbędnym zakresie
także analizy ich oprogramowania układowego. Celem było wyłącznie
uzyskanie współdziałania z innym oprogramowaniem, w granicach, na jakie
pozwala art. 75 ust. 2 pkt 2 i 3 ustawy o prawie autorskim i prawach
pokrewnych (wdrażający art. 5 ust. 3 i art. 6 dyrektywy 2009/24/WE).

## Czego repozytorium nie zawiera

- oprogramowania układowego producenta ani jego zrzutów (repozytorium
  zawiera jedynie informacje niezbędne do współdziałania, np. adresy w
  pamięci flash);
- oprogramowania ani danych z serwerów producenta;
- kluczy urządzeń (poza jedną przykładową parą z urządzenia autora
  oryginalnej integracji, opublikowaną wcześniej przez tego autora w historii
  repozytorium). Każde urządzenie ma własne klucze i trzeba je odczytać
  z **własnego** urządzenia (zob.
  [`tools/EXTRACTING_CREDENTIALS.md`](tools/EXTRACTING_CREDENTIALS.md)).

## Tylko własne urządzenia

Używaj tego oprogramowania, a zwłaszcza narzędzi do odczytu kluczy,
wyłącznie na urządzeniach, które są Twoją własnością albo do których masz
wyraźne upoważnienie. Bez kluczy danego urządzenia integracja nie odczyta
z niego danych. Nie próbuj zdobywać kluczy cudzych urządzeń.

## Ryzyko i brak gwarancji

Odczyt kluczy wymaga podłączenia debuggera do układu w urządzeniu. Można
przy tym trwale unieruchomić urządzenie, a oprogramowanie układowe ma
znane cechy, które zwiększają to ryzyko, jeśli procedura nie zostanie
wykonana dokładnie. Jeśli to możliwe, najpierw zrób pełną kopię pamięci
flash.

Oprogramowanie jest udostępniane **„tak jak jest”, bez jakiejkolwiek
gwarancji**, na [licencji MIT](LICENSE). Autorzy nie odpowiadają za
uszkodzone urządzenia, utracone dane, błędne odczyty ani inne skutki jego
użycia. Nie używaj tych odczytów do rozliczeń ze sprzedawcą energii.

## Prywatność

Wszystko działa lokalnie. Integracja łączy się z urządzeniem przez
Bluetooth i zapisuje dane wyłącznie w Twoim Home Assistancie. Nie wysyła
niczego autorom ani komukolwiek innemu.

## Uprawnieni

Jeśli przysługują Ci prawa do produktu OneMeter i masz zastrzeżenia do
tego projektu, załóż zgłoszenie (issue) w repozytorium. Rozpatrzymy je.

---

# Legal notice

This is not legal advice. It describes what this project is, what it
contains, and the terms on which it is offered.

## No affiliation

This is an independent community project. It is **not affiliated with,
endorsed by, or supported by OneMeter sp. z o.o.**, its bankruptcy
estate, or any successor. "OneMeter" and related names and logos belong
to their respective owners; they are used here only to identify the
hardware this software is compatible with.

## Why it exists and how it was made

The OneMeter reader depends on a vendor cloud that shut down when the
company went bankrupt, leaving working devices that their owners could no
longer use. This integration restores **interoperability** between those
devices and Home Assistant.

The protocol was worked out independently by the project's authors
(including the author of the original integration), by observing,
studying and testing devices they own and, where necessary, analysing
those devices' firmware. The sole purpose was to make the devices work
with other software, within the limits of Directive 2009/24/EC, Articles
5(3) and 6, implemented in Poland by Article 75(2)(2) and (3) of the
Copyright and Related Rights Act (*ustawa o prawie autorskim i prawach
pokrewnych*).

## What this repository does not contain

- no vendor firmware or firmware dumps (only the facts needed for
  interoperability, such as flash addresses);
- no vendor server software or data;
- no device keys (apart from one example pair from the original
  author's own device, published earlier by that author in the repository
  history). Every device has its own keys, and you have to read
  them from **your own** device (see
  [`tools/EXTRACTING_CREDENTIALS.md`](tools/EXTRACTING_CREDENTIALS.md)).

## Use it on your own devices

Use this software, and the credential tooling in particular, only on
devices you own or are explicitly authorised to work on. The integration
cannot read a device without that device's keys, and you should not try
to obtain keys for devices that are not yours.

## Risk and warranty

Reading credentials means attaching a debugger to the device's chip. It
is possible to leave a device unusable, and the device's own firmware
has known quirks that make this more likely if the procedure is not
followed exactly. Take a full flash backup first if you can.

The software is provided **"as is", without warranty of any kind**,
under the [MIT License](LICENSE). The authors are not liable for damaged
devices, lost data, incorrect readings, or any other consequence of using
it. Do not use its readings for billing or settlement with your energy
supplier.

## Privacy

Everything runs locally. The integration talks to your device over
Bluetooth and stores data only in your Home Assistant instance. It sends
nothing to the authors or to any third party.

## Rights holders

If you hold rights in the OneMeter product and have a concern about this
project, please open an issue on the repository. We will review it.
