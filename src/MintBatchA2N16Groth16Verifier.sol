// SPDX-License-Identifier: GPL-3.0
/*
    Copyright 2021 0KIMS association.

    This file is generated with [snarkJS](https://github.com/iden3/snarkjs).

    snarkJS is a free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    snarkJS is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
    or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
    License for more details.

    You should have received a copy of the GNU General Public License
    along with snarkJS. If not, see <https://www.gnu.org/licenses/>.
*/

pragma solidity >=0.7.0 <0.9.0;

contract MintBatchA2N16Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 7978374530620261980907563326553864501965289527526872291934547527166537747551;
    uint256 constant alphay  = 5901113449839617362854698006124082792885893177043162830865317277785118545420;
    uint256 constant betax1  = 18220824476844239577768066887046680990910624075694529208134813320434359817912;
    uint256 constant betax2  = 16450672241897021868604598054257760272991953374496313798435261611576668251269;
    uint256 constant betay1  = 11726201621433110351792550440521544935687171988284287970664993912055275635786;
    uint256 constant betay2  = 20979770059934782849226372365594771099262336599978723952245345619213052609192;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 13825023949756639527008135151436020790543011719288607378338853543632597737346;
    uint256 constant deltax2 = 18259557920579726918927638460641179780952286734432128308056746632939794541766;
    uint256 constant deltay1 = 1455962535151580691328662807548584341804411591276043365401738296510837037166;
    uint256 constant deltay2 = 5394133613890769119390348758228456894527230063901023256673728542297405183251;

    
    uint256 constant IC0x = 15548654854193929826710350969304338769459069163629310693203955597599682023651;
    uint256 constant IC0y = 11330089580802080236104207720264433341958394477240092258451381897661105469382;
    
    uint256 constant IC1x = 15494254682784739786296947098827670528035555480317239946634174643231540933747;
    uint256 constant IC1y = 20660315672956497570640282320541352247288029038429656877126152560922231973093;
    
    uint256 constant IC2x = 11784597027711299616612398637461060561796101870813126083767363792129058162447;
    uint256 constant IC2y = 14510124910739262512929698970231283220881610082351266263147248708129218883213;
    
    uint256 constant IC3x = 13095941168805515018291913782946333873626019712257511783703888074560746585520;
    uint256 constant IC3y = 7927338475481116347009363339773387726937627570330551238500310857280312818910;
    
    uint256 constant IC4x = 12890902349833216716283735572716623173611681080146594559998210375379466409835;
    uint256 constant IC4y = 13027154366915645798951346792140163873884720794662471842366535468874714961309;
    
    uint256 constant IC5x = 1265768111529131272669194508229849140621779752909808009385531299037953984957;
    uint256 constant IC5y = 4335320844394615033106461265236852294620325732653308864656996229939576667425;
    
    uint256 constant IC6x = 4971380884165384789371919063155111316102897463859090660955817098472944052008;
    uint256 constant IC6y = 17566203649271416116046368505125795779566204709946615067679603658682104730777;
    
    uint256 constant IC7x = 10315340342918352210702568257206335775904542315769735003106583385028563763692;
    uint256 constant IC7y = 14460325942797031156504844603203764742570551012253060029014900213293331912885;
    
    uint256 constant IC8x = 1833896411552278894779498787116934197023921700582670298765655621613444766206;
    uint256 constant IC8y = 18963071315658459759497596095758813673878626214852509760049982874649970613279;
    
    uint256 constant IC9x = 1480000296379462901006336976777241173791548829969060621343189785505554483378;
    uint256 constant IC9y = 15513107180142993341096720460949843039081561698599574976840270611279562579535;
    
    uint256 constant IC10x = 9660881999714373375559442899500459282257477762062242104893725443427206807560;
    uint256 constant IC10y = 12140154611731017688276338401492063020046692399117454377316396547552647519529;
    
    uint256 constant IC11x = 4442334020878626576610515076468234892366942327778928116844858223555977990130;
    uint256 constant IC11y = 11928924316720879950966983184039578803580524967144513582554387268219560695446;
    
    uint256 constant IC12x = 1204997894372763420420076405587685285840269876739118787960541590621269874589;
    uint256 constant IC12y = 20868383729949050806408361873200679574073538053234196691125536870087453262713;
    
    uint256 constant IC13x = 21125776492993096084465688136332896044899601778832889389029511148820116470845;
    uint256 constant IC13y = 19756195022709279042601424034879547313645899225643378910290990197201268056286;
    
    uint256 constant IC14x = 672677946193348202816206249853755189204772588920782961152843257284068165964;
    uint256 constant IC14y = 20851268906362685431944406882437033729662818049074470105359749126936363548418;
    
    uint256 constant IC15x = 11184512579606072940077948913683138370956896704945391206013449193140401490757;
    uint256 constant IC15y = 9067892351416000876654119738204836579173123711881954851569872182751339881427;
    
    uint256 constant IC16x = 3226304833994781247109493774877094442824287520031824099381945347644042944306;
    uint256 constant IC16y = 20055658298150324031147941011727314624280187379314667412679359135642684458668;
    
    uint256 constant IC17x = 9082210067326523292080012185475597194574704964055003685991695855644382389577;
    uint256 constant IC17y = 16577034923731384259225388755309567870887403092210482929480702883776747078632;
    
    uint256 constant IC18x = 16785303083046554557312011336059726386993846880614391992255075205545310468082;
    uint256 constant IC18y = 19793759776693888127561155658635431797705589316836602294802871635849683569608;
    
    uint256 constant IC19x = 18597743144164383570795040445927539102985169255518783531266649464635056619487;
    uint256 constant IC19y = 12409817501051044331889653686170980690505310844837612956744348300526565078788;
    
    uint256 constant IC20x = 12871077833781279005452067462054034705404176026083886480647042015257637367759;
    uint256 constant IC20y = 1026122244878698820968384354678269299298754006136414440221662927094337925770;
    
    uint256 constant IC21x = 991883025863670463157375323574707943436316895963973653989958746032734624075;
    uint256 constant IC21y = 13123177449752055525560675659945522501580135161501987354162074510059202539595;
    
    uint256 constant IC22x = 18400623956943080542197278030273719891848082539956879623578128744136610699053;
    uint256 constant IC22y = 20284303483663594793697080987820878722623556505497948146360093056533322838085;
    
    uint256 constant IC23x = 19916726396972237297455417807789022955890087227257879889719588366124127471235;
    uint256 constant IC23y = 8928632132276136879630710097270964158822330969201677649308645302513911438902;
    
    uint256 constant IC24x = 19479003421214114985779104003541841910159424338175047488009732707371893590664;
    uint256 constant IC24y = 16194832596400234469150366320167467984331849471643261342527462273473740492879;
    
    uint256 constant IC25x = 15625060082862112718848044326755578895618401373371769478427770731570886918031;
    uint256 constant IC25y = 21754293207730262858513351191354427498672086405280359041873280778474561867451;
    
    uint256 constant IC26x = 10249420897787506325282967787270691998648803720790657548337561786659822899727;
    uint256 constant IC26y = 16284377646655880696039859347909486038555437181901050246331968714047042314309;
    
    uint256 constant IC27x = 9266507604688388942162083452659330937753991014021741586466722879176901927268;
    uint256 constant IC27y = 11434269463826755038355212804416951529268910734460563225533390940184803187189;
    
    uint256 constant IC28x = 7172922352018098845237586327673704438301544543877452930696604412154948703411;
    uint256 constant IC28y = 3317344211866070443225783171277245365999769208472812973326172910172913874913;
    
    uint256 constant IC29x = 17946177516248911179871607236487407319323339498697821336246588715645026946061;
    uint256 constant IC29y = 13370652168504655757493363562539633941631234758053269474210669968991067903669;
    
    uint256 constant IC30x = 862723060675356066838596150087604148118092090487674759893771840030669556572;
    uint256 constant IC30y = 10026094010006463382755598049046486827202710996042794135839958999110726903777;
    
    uint256 constant IC31x = 10847035518059632389624405489434159253226464323697904230140441559320131059676;
    uint256 constant IC31y = 6378811290576733415138170417403494386851148941047820256105067508807885933134;
    
    uint256 constant IC32x = 8081258326243772700070967093366214374958543009151964392472194489249836150192;
    uint256 constant IC32y = 15462943823804871427063493144070882136584598111187201682032253183606634469090;
    
    uint256 constant IC33x = 21672349114233421257838980402234915889669636388146285358829861589736825557551;
    uint256 constant IC33y = 6330276695701124083577946685111327809672145436952630352860630584070858130986;
    
    uint256 constant IC34x = 21207810416746854940617507750420249583318542300644207535560369546947764514731;
    uint256 constant IC34y = 18803411244196314241452518487878712863745072033158966961283631986563700383679;
    
    uint256 constant IC35x = 780059977658255673648580845618410506303645029725296273764080401776015925744;
    uint256 constant IC35y = 21848193985354224335615528028209538996691418748247532704135991271358667075088;
    
    uint256 constant IC36x = 18631383161396585458527171546970331270424790200473729163189533042728977239992;
    uint256 constant IC36y = 13410800327060954227654607597424196537081446798898713293498561779049378448489;
    
    uint256 constant IC37x = 6014369095622204790397711484997108588154802518061879175953730019808502228278;
    uint256 constant IC37y = 6739977112666815991553261510457738673934028747572045859052915267389385660415;
    
    uint256 constant IC38x = 18635162079230859107153414945870622119709609525582109666948819726069227796711;
    uint256 constant IC38y = 15231333623324305702620946744120999472907927548163638403209133141552050126982;
    
    uint256 constant IC39x = 15068813208881644948594323442596362450304568252669255434845216710778681682913;
    uint256 constant IC39y = 1959728077044316307190599343632080722482181574961269688230468504591422839994;
    
    uint256 constant IC40x = 19501432436052869759430512371158409890872786070531128216013689006500412584387;
    uint256 constant IC40y = 2573790374440924761853886918391406070025414417799424067414940769992588068196;
    
    uint256 constant IC41x = 14717344425908993172084467696038995797353989820973023730156377802049358721103;
    uint256 constant IC41y = 20761074400907861778050908270920242276226377034159854145257702418645232414079;
    
    uint256 constant IC42x = 16566096077690782412798082480401034798673618230381059156701203383728298945736;
    uint256 constant IC42y = 15371020692007001590335957603697420040881070654944939260108283768412663740845;
    
    uint256 constant IC43x = 14731503419976285586419489705187306488386690001459544804760597578590223486310;
    uint256 constant IC43y = 14714624536787518778681815057952329853892417005733972783819324598006670289829;
    
    uint256 constant IC44x = 17783543760250547212279876756670974440023574504534733057078358771550335468350;
    uint256 constant IC44y = 12560465031802123954428239596461546040195551812193788167688610705951097256839;
    
    uint256 constant IC45x = 20996105422522176781650072916615023307268828414678319895077539402458578176246;
    uint256 constant IC45y = 15389244194526609350914871655171046106166109744467467075200231722997184219426;
    
    uint256 constant IC46x = 5487457001443393149855744490209810729376241057178817487526054087400837074070;
    uint256 constant IC46y = 7239432053564326147201738357964058759319731510522335116138363012726392307920;
    
    uint256 constant IC47x = 14960610418116379752728797012642434403265295044919388583574129527987154320875;
    uint256 constant IC47y = 13756730651421100021165263810005932847889953414701834803133079755232518605415;
    
    uint256 constant IC48x = 15429454920514435169552833282461715188498947808182299684236502883934920911247;
    uint256 constant IC48y = 3061189117411912453003374272571409156244319919381990396047389748205446944363;
    
    uint256 constant IC49x = 11981798101434770217033059350712141261547789455775435341959566642762621835048;
    uint256 constant IC49y = 18600548409554160058129797777404422121630343192776339197324946039493205892317;
    
    uint256 constant IC50x = 8543413844900636825629794127963305617216408630984135390441981565718156559743;
    uint256 constant IC50y = 8350242089951058511368894683353427824596129326771176453109242458612672559499;
    
    uint256 constant IC51x = 3354713588758704904725275255694920365621815926506556154732386383096965526685;
    uint256 constant IC51y = 15606549330928017347100995739879530979339718495694906093528172913481094287757;
    
    uint256 constant IC52x = 235049135144825978925999552553281151432992394354969078956648605397247591984;
    uint256 constant IC52y = 7376270229611336042762560858410800754205253462186782850698634662676658260369;
    
    uint256 constant IC53x = 4410134432541860934862216394426123874494327018024064778137824504306965346081;
    uint256 constant IC53y = 17179677047968724185707884840282355384322744890329553654996185337465305351397;
    
    uint256 constant IC54x = 6693310371191449044371376346150409230300771914937602799994969540583471190288;
    uint256 constant IC54y = 16899401536834225373423009010402638436132382855967447317192312279633502544645;
    
    uint256 constant IC55x = 11951704934656277675888591382155858409481147306394246801486225671488194016087;
    uint256 constant IC55y = 12195275413332881830296349331856729997623448227736966749893740069389135105979;
    
    uint256 constant IC56x = 11041629487920635955235315977794664032345145688379477845459231595087348192041;
    uint256 constant IC56y = 18546927443350751161396262576085336783453586718077167368259028356704799003691;
    
    uint256 constant IC57x = 21465722397363498029976959343739068352342551079868855638908064571976288712997;
    uint256 constant IC57y = 18293214537515404876019994470484207561770154669438827557009693299333420862707;
    
    uint256 constant IC58x = 6983926135022134440863554069366878648520065234871047019619880007597335753679;
    uint256 constant IC58y = 12642690916392044934318157406394568034125098113509999623334424428319718311442;
    
    uint256 constant IC59x = 19722667806465832175012875075872972727077984354297396737517848379513319435879;
    uint256 constant IC59y = 12230635825067720124654138720378035322027208144030965685816924887055340968939;
    
    uint256 constant IC60x = 19510213254314421539646143311798511011098562009750973622797490818630885439069;
    uint256 constant IC60y = 14916738984481655132412002501629505499195256718045677076474067073574195145454;
    
    uint256 constant IC61x = 15084201112515180651650042939465037206168231670697486626517914696151354603252;
    uint256 constant IC61y = 21475976816303984422194581848152295622733718986966534396945492973688870605535;
    
    uint256 constant IC62x = 2732778216958172171616525668773614626416657399821110128000854646189123805418;
    uint256 constant IC62y = 13051140695374770826437673329382369050967956346322988294932927873268780434717;
    
    uint256 constant IC63x = 2144751875479752000930155173806451916318462194396003843470590940508028326983;
    uint256 constant IC63y = 14523381705817061497776236798304869645963882414142663953192911878238000244960;
    
    uint256 constant IC64x = 12621130940868330410710443075801612521707758609585790378412171215318509451198;
    uint256 constant IC64y = 13246555177652094808618166837235749195177560941497883907783247463291959437182;
    
    uint256 constant IC65x = 16778547136739420547880456458370715558451425538446183494561009027912981272819;
    uint256 constant IC65y = 4256760910938343227335841996417281724035343132280118629086882528164936956231;
    
    uint256 constant IC66x = 14488466715633360960058108779043449964236839528767619204178783750032209037196;
    uint256 constant IC66y = 15132137287547823974887150268500278534127762428391491645854537011906398378375;
    
    uint256 constant IC67x = 6579346196517860442286765255033335948470669128416235193597876681098295841516;
    uint256 constant IC67y = 18540576239735592095260514641975809749381857731570830213569407607997175805552;
    
    uint256 constant IC68x = 11519723695603842021490338210571014401350708386175004347905486194868984784882;
    uint256 constant IC68y = 16032766056146287783026034695166871782463175799918396920450042303781750303608;
    
    uint256 constant IC69x = 21523917552370632704236261743953588223776195179067700496356801561796457857447;
    uint256 constant IC69y = 4202210419591951459838747890569037655800211543007263407996434708515406460924;
    
    uint256 constant IC70x = 19228971215327926868937292149291863520219662904492778087102624477178023245334;
    uint256 constant IC70y = 6321047718017166835279698642988077480091589680880604584731503757481663776918;
    
    uint256 constant IC71x = 11292166610109898326389572370776895453852896536041032801162616027721331098998;
    uint256 constant IC71y = 4000797906244717813519441974172172240816132492797327212475608486260254963974;
    
    uint256 constant IC72x = 15013571461952378058671239261796056674985748560057590694466823050773282181734;
    uint256 constant IC72y = 1927475827054087210065817509036315617504639563956324149551707568107714032477;
    
    uint256 constant IC73x = 12919825430819123987773621948702711026683657625112102642100451869037462109713;
    uint256 constant IC73y = 20756229480593722931997586598524333153811536915972810288389743260478449744123;
    
    uint256 constant IC74x = 13775474029887716678169331748720266755435132140825496490672498107342360576704;
    uint256 constant IC74y = 15797824238037369988584770377299723910790722497554460691849487187770853287555;
    
    uint256 constant IC75x = 3628714448229743096068983081745510760839364278099425681531761399143831224162;
    uint256 constant IC75y = 9033890236743958366885996739412460901582916001145698166834592005670320696213;
    
    uint256 constant IC76x = 3024901439432612014280781837981524624373526821591634062966019878157844716328;
    uint256 constant IC76y = 14507913531769423516351966613754308708566609287473291300234158622039350036244;
    
    uint256 constant IC77x = 15460543600862858455686238198680188101105373983999072550515482209773891415380;
    uint256 constant IC77y = 17013309017193754262105527329982505632519426559370051970350095855996661272128;
    
    uint256 constant IC78x = 2002467603739795662034183977380197305036936700336331822962036247753006500647;
    uint256 constant IC78y = 3508087962158347555748817662275890382655984454567352381594116738787459780927;
    
    uint256 constant IC79x = 11738556004090734705094596583544262043200848726743586264658240220993354453308;
    uint256 constant IC79y = 3235561752024732779338821286430441404867994853614005062280506539698001905497;
    
    uint256 constant IC80x = 11904105859689049713848265564242265987327947664093245008597367009140747703774;
    uint256 constant IC80y = 8852004518304971245056013418812311892394855148557181902735200218969165366118;
    
    uint256 constant IC81x = 2067552085713473952256260978243193782065681674372222812048239253731017889611;
    uint256 constant IC81y = 9888996085262313873020198808118805771874097954474415409703686860113493624699;
    
    uint256 constant IC82x = 715317650101414003599081553727251479956151832045219133004324328810648250171;
    uint256 constant IC82y = 3408485665840947440582666574974529998978821669552298878407801852365858465649;
    
    uint256 constant IC83x = 13815378032745631270677215636611057518112054066578869397953125404397126311398;
    uint256 constant IC83y = 8063432772947510279737930946136528014520740148014418019115946672031916530641;
    
    uint256 constant IC84x = 21788633172466067104311984977258397981658847019193219245216086714593018530733;
    uint256 constant IC84y = 12874887439065055375500360254725035903312149334988399803276408118521847491211;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[84] calldata _pubSignals) public returns (bool) {
        assembly {
            function checkField(v) {
                if iszero(lt(v, r)) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }
            
            // G1 function to multiply a G1 value(x,y) to value in an address
            function g1_mulAccC(pR, x, y, s) {
                let success
                let mIn := mload(0x40)
                mstore(mIn, x)
                mstore(add(mIn, 32), y)
                mstore(add(mIn, 64), s)

                success := staticcall(sub(gas(), 2000), 7, mIn, 96, mIn, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }

                mstore(add(mIn, 64), mload(pR))
                mstore(add(mIn, 96), mload(add(pR, 32)))

                success := staticcall(sub(gas(), 2000), 6, mIn, 128, pR, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }

            function checkPairing(pA, pB, pC, pubSignals, pMem) -> isOk {
                let _pPairing := add(pMem, pPairing)
                let _pVk := add(pMem, pVk)

                mstore(_pVk, IC0x)
                mstore(add(_pVk, 32), IC0y)

                // Compute the linear combination vk_x
                
                g1_mulAccC(_pVk, IC1x, IC1y, calldataload(add(pubSignals, 0)))
                
                g1_mulAccC(_pVk, IC2x, IC2y, calldataload(add(pubSignals, 32)))
                
                g1_mulAccC(_pVk, IC3x, IC3y, calldataload(add(pubSignals, 64)))
                
                g1_mulAccC(_pVk, IC4x, IC4y, calldataload(add(pubSignals, 96)))
                
                g1_mulAccC(_pVk, IC5x, IC5y, calldataload(add(pubSignals, 128)))
                
                g1_mulAccC(_pVk, IC6x, IC6y, calldataload(add(pubSignals, 160)))
                
                g1_mulAccC(_pVk, IC7x, IC7y, calldataload(add(pubSignals, 192)))
                
                g1_mulAccC(_pVk, IC8x, IC8y, calldataload(add(pubSignals, 224)))
                
                g1_mulAccC(_pVk, IC9x, IC9y, calldataload(add(pubSignals, 256)))
                
                g1_mulAccC(_pVk, IC10x, IC10y, calldataload(add(pubSignals, 288)))
                
                g1_mulAccC(_pVk, IC11x, IC11y, calldataload(add(pubSignals, 320)))
                
                g1_mulAccC(_pVk, IC12x, IC12y, calldataload(add(pubSignals, 352)))
                
                g1_mulAccC(_pVk, IC13x, IC13y, calldataload(add(pubSignals, 384)))
                
                g1_mulAccC(_pVk, IC14x, IC14y, calldataload(add(pubSignals, 416)))
                
                g1_mulAccC(_pVk, IC15x, IC15y, calldataload(add(pubSignals, 448)))
                
                g1_mulAccC(_pVk, IC16x, IC16y, calldataload(add(pubSignals, 480)))
                
                g1_mulAccC(_pVk, IC17x, IC17y, calldataload(add(pubSignals, 512)))
                
                g1_mulAccC(_pVk, IC18x, IC18y, calldataload(add(pubSignals, 544)))
                
                g1_mulAccC(_pVk, IC19x, IC19y, calldataload(add(pubSignals, 576)))
                
                g1_mulAccC(_pVk, IC20x, IC20y, calldataload(add(pubSignals, 608)))
                
                g1_mulAccC(_pVk, IC21x, IC21y, calldataload(add(pubSignals, 640)))
                
                g1_mulAccC(_pVk, IC22x, IC22y, calldataload(add(pubSignals, 672)))
                
                g1_mulAccC(_pVk, IC23x, IC23y, calldataload(add(pubSignals, 704)))
                
                g1_mulAccC(_pVk, IC24x, IC24y, calldataload(add(pubSignals, 736)))
                
                g1_mulAccC(_pVk, IC25x, IC25y, calldataload(add(pubSignals, 768)))
                
                g1_mulAccC(_pVk, IC26x, IC26y, calldataload(add(pubSignals, 800)))
                
                g1_mulAccC(_pVk, IC27x, IC27y, calldataload(add(pubSignals, 832)))
                
                g1_mulAccC(_pVk, IC28x, IC28y, calldataload(add(pubSignals, 864)))
                
                g1_mulAccC(_pVk, IC29x, IC29y, calldataload(add(pubSignals, 896)))
                
                g1_mulAccC(_pVk, IC30x, IC30y, calldataload(add(pubSignals, 928)))
                
                g1_mulAccC(_pVk, IC31x, IC31y, calldataload(add(pubSignals, 960)))
                
                g1_mulAccC(_pVk, IC32x, IC32y, calldataload(add(pubSignals, 992)))
                
                g1_mulAccC(_pVk, IC33x, IC33y, calldataload(add(pubSignals, 1024)))
                
                g1_mulAccC(_pVk, IC34x, IC34y, calldataload(add(pubSignals, 1056)))
                
                g1_mulAccC(_pVk, IC35x, IC35y, calldataload(add(pubSignals, 1088)))
                
                g1_mulAccC(_pVk, IC36x, IC36y, calldataload(add(pubSignals, 1120)))
                
                g1_mulAccC(_pVk, IC37x, IC37y, calldataload(add(pubSignals, 1152)))
                
                g1_mulAccC(_pVk, IC38x, IC38y, calldataload(add(pubSignals, 1184)))
                
                g1_mulAccC(_pVk, IC39x, IC39y, calldataload(add(pubSignals, 1216)))
                
                g1_mulAccC(_pVk, IC40x, IC40y, calldataload(add(pubSignals, 1248)))
                
                g1_mulAccC(_pVk, IC41x, IC41y, calldataload(add(pubSignals, 1280)))
                
                g1_mulAccC(_pVk, IC42x, IC42y, calldataload(add(pubSignals, 1312)))
                
                g1_mulAccC(_pVk, IC43x, IC43y, calldataload(add(pubSignals, 1344)))
                
                g1_mulAccC(_pVk, IC44x, IC44y, calldataload(add(pubSignals, 1376)))
                
                g1_mulAccC(_pVk, IC45x, IC45y, calldataload(add(pubSignals, 1408)))
                
                g1_mulAccC(_pVk, IC46x, IC46y, calldataload(add(pubSignals, 1440)))
                
                g1_mulAccC(_pVk, IC47x, IC47y, calldataload(add(pubSignals, 1472)))
                
                g1_mulAccC(_pVk, IC48x, IC48y, calldataload(add(pubSignals, 1504)))
                
                g1_mulAccC(_pVk, IC49x, IC49y, calldataload(add(pubSignals, 1536)))
                
                g1_mulAccC(_pVk, IC50x, IC50y, calldataload(add(pubSignals, 1568)))
                
                g1_mulAccC(_pVk, IC51x, IC51y, calldataload(add(pubSignals, 1600)))
                
                g1_mulAccC(_pVk, IC52x, IC52y, calldataload(add(pubSignals, 1632)))
                
                g1_mulAccC(_pVk, IC53x, IC53y, calldataload(add(pubSignals, 1664)))
                
                g1_mulAccC(_pVk, IC54x, IC54y, calldataload(add(pubSignals, 1696)))
                
                g1_mulAccC(_pVk, IC55x, IC55y, calldataload(add(pubSignals, 1728)))
                
                g1_mulAccC(_pVk, IC56x, IC56y, calldataload(add(pubSignals, 1760)))
                
                g1_mulAccC(_pVk, IC57x, IC57y, calldataload(add(pubSignals, 1792)))
                
                g1_mulAccC(_pVk, IC58x, IC58y, calldataload(add(pubSignals, 1824)))
                
                g1_mulAccC(_pVk, IC59x, IC59y, calldataload(add(pubSignals, 1856)))
                
                g1_mulAccC(_pVk, IC60x, IC60y, calldataload(add(pubSignals, 1888)))
                
                g1_mulAccC(_pVk, IC61x, IC61y, calldataload(add(pubSignals, 1920)))
                
                g1_mulAccC(_pVk, IC62x, IC62y, calldataload(add(pubSignals, 1952)))
                
                g1_mulAccC(_pVk, IC63x, IC63y, calldataload(add(pubSignals, 1984)))
                
                g1_mulAccC(_pVk, IC64x, IC64y, calldataload(add(pubSignals, 2016)))
                
                g1_mulAccC(_pVk, IC65x, IC65y, calldataload(add(pubSignals, 2048)))
                
                g1_mulAccC(_pVk, IC66x, IC66y, calldataload(add(pubSignals, 2080)))
                
                g1_mulAccC(_pVk, IC67x, IC67y, calldataload(add(pubSignals, 2112)))
                
                g1_mulAccC(_pVk, IC68x, IC68y, calldataload(add(pubSignals, 2144)))
                
                g1_mulAccC(_pVk, IC69x, IC69y, calldataload(add(pubSignals, 2176)))
                
                g1_mulAccC(_pVk, IC70x, IC70y, calldataload(add(pubSignals, 2208)))
                
                g1_mulAccC(_pVk, IC71x, IC71y, calldataload(add(pubSignals, 2240)))
                
                g1_mulAccC(_pVk, IC72x, IC72y, calldataload(add(pubSignals, 2272)))
                
                g1_mulAccC(_pVk, IC73x, IC73y, calldataload(add(pubSignals, 2304)))
                
                g1_mulAccC(_pVk, IC74x, IC74y, calldataload(add(pubSignals, 2336)))
                
                g1_mulAccC(_pVk, IC75x, IC75y, calldataload(add(pubSignals, 2368)))
                
                g1_mulAccC(_pVk, IC76x, IC76y, calldataload(add(pubSignals, 2400)))
                
                g1_mulAccC(_pVk, IC77x, IC77y, calldataload(add(pubSignals, 2432)))
                
                g1_mulAccC(_pVk, IC78x, IC78y, calldataload(add(pubSignals, 2464)))
                
                g1_mulAccC(_pVk, IC79x, IC79y, calldataload(add(pubSignals, 2496)))
                
                g1_mulAccC(_pVk, IC80x, IC80y, calldataload(add(pubSignals, 2528)))
                
                g1_mulAccC(_pVk, IC81x, IC81y, calldataload(add(pubSignals, 2560)))
                
                g1_mulAccC(_pVk, IC82x, IC82y, calldataload(add(pubSignals, 2592)))
                
                g1_mulAccC(_pVk, IC83x, IC83y, calldataload(add(pubSignals, 2624)))
                
                g1_mulAccC(_pVk, IC84x, IC84y, calldataload(add(pubSignals, 2656)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(pB))
                mstore(add(_pPairing, 96), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 128), calldataload(add(pB, 64)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 96)))

                // alpha1
                mstore(add(_pPairing, 192), alphax)
                mstore(add(_pPairing, 224), alphay)

                // beta2
                mstore(add(_pPairing, 256), betax1)
                mstore(add(_pPairing, 288), betax2)
                mstore(add(_pPairing, 320), betay1)
                mstore(add(_pPairing, 352), betay2)

                // vk_x
                mstore(add(_pPairing, 384), mload(add(pMem, pVk)))
                mstore(add(_pPairing, 416), mload(add(pMem, add(pVk, 32))))


                // gamma2
                mstore(add(_pPairing, 448), gammax1)
                mstore(add(_pPairing, 480), gammax2)
                mstore(add(_pPairing, 512), gammay1)
                mstore(add(_pPairing, 544), gammay2)

                // C
                mstore(add(_pPairing, 576), calldataload(pC))
                mstore(add(_pPairing, 608), calldataload(add(pC, 32)))

                // delta2
                mstore(add(_pPairing, 640), deltax1)
                mstore(add(_pPairing, 672), deltax2)
                mstore(add(_pPairing, 704), deltay1)
                mstore(add(_pPairing, 736), deltay2)


                let success := staticcall(sub(gas(), 2000), 8, _pPairing, 768, _pPairing, 0x20)

                isOk := and(success, mload(_pPairing))
            }

            let pMem := mload(0x40)
            mstore(0x40, add(pMem, pLastMem))

            // Validate that all evaluations ∈ F
            
            checkField(calldataload(add(_pubSignals, 0)))
            
            checkField(calldataload(add(_pubSignals, 32)))
            
            checkField(calldataload(add(_pubSignals, 64)))
            
            checkField(calldataload(add(_pubSignals, 96)))
            
            checkField(calldataload(add(_pubSignals, 128)))
            
            checkField(calldataload(add(_pubSignals, 160)))
            
            checkField(calldataload(add(_pubSignals, 192)))
            
            checkField(calldataload(add(_pubSignals, 224)))
            
            checkField(calldataload(add(_pubSignals, 256)))
            
            checkField(calldataload(add(_pubSignals, 288)))
            
            checkField(calldataload(add(_pubSignals, 320)))
            
            checkField(calldataload(add(_pubSignals, 352)))
            
            checkField(calldataload(add(_pubSignals, 384)))
            
            checkField(calldataload(add(_pubSignals, 416)))
            
            checkField(calldataload(add(_pubSignals, 448)))
            
            checkField(calldataload(add(_pubSignals, 480)))
            
            checkField(calldataload(add(_pubSignals, 512)))
            
            checkField(calldataload(add(_pubSignals, 544)))
            
            checkField(calldataload(add(_pubSignals, 576)))
            
            checkField(calldataload(add(_pubSignals, 608)))
            
            checkField(calldataload(add(_pubSignals, 640)))
            
            checkField(calldataload(add(_pubSignals, 672)))
            
            checkField(calldataload(add(_pubSignals, 704)))
            
            checkField(calldataload(add(_pubSignals, 736)))
            
            checkField(calldataload(add(_pubSignals, 768)))
            
            checkField(calldataload(add(_pubSignals, 800)))
            
            checkField(calldataload(add(_pubSignals, 832)))
            
            checkField(calldataload(add(_pubSignals, 864)))
            
            checkField(calldataload(add(_pubSignals, 896)))
            
            checkField(calldataload(add(_pubSignals, 928)))
            
            checkField(calldataload(add(_pubSignals, 960)))
            
            checkField(calldataload(add(_pubSignals, 992)))
            
            checkField(calldataload(add(_pubSignals, 1024)))
            
            checkField(calldataload(add(_pubSignals, 1056)))
            
            checkField(calldataload(add(_pubSignals, 1088)))
            
            checkField(calldataload(add(_pubSignals, 1120)))
            
            checkField(calldataload(add(_pubSignals, 1152)))
            
            checkField(calldataload(add(_pubSignals, 1184)))
            
            checkField(calldataload(add(_pubSignals, 1216)))
            
            checkField(calldataload(add(_pubSignals, 1248)))
            
            checkField(calldataload(add(_pubSignals, 1280)))
            
            checkField(calldataload(add(_pubSignals, 1312)))
            
            checkField(calldataload(add(_pubSignals, 1344)))
            
            checkField(calldataload(add(_pubSignals, 1376)))
            
            checkField(calldataload(add(_pubSignals, 1408)))
            
            checkField(calldataload(add(_pubSignals, 1440)))
            
            checkField(calldataload(add(_pubSignals, 1472)))
            
            checkField(calldataload(add(_pubSignals, 1504)))
            
            checkField(calldataload(add(_pubSignals, 1536)))
            
            checkField(calldataload(add(_pubSignals, 1568)))
            
            checkField(calldataload(add(_pubSignals, 1600)))
            
            checkField(calldataload(add(_pubSignals, 1632)))
            
            checkField(calldataload(add(_pubSignals, 1664)))
            
            checkField(calldataload(add(_pubSignals, 1696)))
            
            checkField(calldataload(add(_pubSignals, 1728)))
            
            checkField(calldataload(add(_pubSignals, 1760)))
            
            checkField(calldataload(add(_pubSignals, 1792)))
            
            checkField(calldataload(add(_pubSignals, 1824)))
            
            checkField(calldataload(add(_pubSignals, 1856)))
            
            checkField(calldataload(add(_pubSignals, 1888)))
            
            checkField(calldataload(add(_pubSignals, 1920)))
            
            checkField(calldataload(add(_pubSignals, 1952)))
            
            checkField(calldataload(add(_pubSignals, 1984)))
            
            checkField(calldataload(add(_pubSignals, 2016)))
            
            checkField(calldataload(add(_pubSignals, 2048)))
            
            checkField(calldataload(add(_pubSignals, 2080)))
            
            checkField(calldataload(add(_pubSignals, 2112)))
            
            checkField(calldataload(add(_pubSignals, 2144)))
            
            checkField(calldataload(add(_pubSignals, 2176)))
            
            checkField(calldataload(add(_pubSignals, 2208)))
            
            checkField(calldataload(add(_pubSignals, 2240)))
            
            checkField(calldataload(add(_pubSignals, 2272)))
            
            checkField(calldataload(add(_pubSignals, 2304)))
            
            checkField(calldataload(add(_pubSignals, 2336)))
            
            checkField(calldataload(add(_pubSignals, 2368)))
            
            checkField(calldataload(add(_pubSignals, 2400)))
            
            checkField(calldataload(add(_pubSignals, 2432)))
            
            checkField(calldataload(add(_pubSignals, 2464)))
            
            checkField(calldataload(add(_pubSignals, 2496)))
            
            checkField(calldataload(add(_pubSignals, 2528)))
            
            checkField(calldataload(add(_pubSignals, 2560)))
            
            checkField(calldataload(add(_pubSignals, 2592)))
            
            checkField(calldataload(add(_pubSignals, 2624)))
            
            checkField(calldataload(add(_pubSignals, 2656)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
