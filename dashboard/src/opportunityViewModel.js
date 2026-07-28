const routeIsEnabled = (opportunity, enabledExchanges) => {
  const buyExchange = opportunity.details?.buy_exchange?.toLowerCase();
  const sellExchange = opportunity.details?.sell_exchange?.toLowerCase();
  return enabledExchanges.has(buyExchange) && enabledExchanges.has(sellExchange);
};

const isBinanceVariationalRoute = (opportunity) => {
  const buyExchange = opportunity.details?.buy_exchange?.toLowerCase();
  const sellExchange = opportunity.details?.sell_exchange?.toLowerCase();
  return (buyExchange === 'binance' && sellExchange === 'variational')
    || (buyExchange === 'variational' && sellExchange === 'binance');
};

const OBSERVATION_PREVIEW_LIMIT = 20;

export const getObservationDisplay = (opportunities, showAll = true) => {
  const visible = showAll
    ? opportunities
    : opportunities.slice(0, OBSERVATION_PREVIEW_LIMIT);

  return {
    visible,
    hiddenCount: opportunities.length - visible.length,
  };
};

export const partitionOpportunities = (opportunities, enabledExchanges) => {
  const executable = [];
  const manual = [];
  const observation = [];

  for (const opportunity of opportunities) {
    if (!routeIsEnabled(opportunity, enabledExchanges)) continue;
    if (opportunity.details?.entry_check_supported === true) {
      executable.push(opportunity);
    } else if (isBinanceVariationalRoute(opportunity)) {
      manual.push(opportunity);
    } else {
      observation.push(opportunity);
    }
  }

  return { executable, manual, observation };
};

export const selectBestReviewCandidates = (
  automaticOpportunities,
  manualOpportunities,
  getScore,
) => {
  const automaticBySymbol = new Map();
  const manualBySymbol = new Map();

  for (const opportunity of automaticOpportunities) {
    const symbol = opportunity.symbol.trim().toUpperCase();
    const current = automaticBySymbol.get(symbol);
    if (!current || getScore(opportunity) > getScore(current)) {
      automaticBySymbol.set(symbol, opportunity);
    }
  }

  for (const opportunity of manualOpportunities) {
    const symbol = opportunity.symbol.trim().toUpperCase();
    const current = manualBySymbol.get(symbol);
    if (!current || getScore(opportunity) > getScore(current)) {
      manualBySymbol.set(symbol, opportunity);
    }
  }

  return new Map([...automaticBySymbol, ...manualBySymbol]);
};
